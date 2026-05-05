"""Agent d'enrichissement LBC pour velos d'occasion - API publique et CLI.

API publique (consommee par lbc-sniper) :
    from enrich_bike import enrich_ad

    result = enrich_ad(
        ad={
            "id": 12345,
            "subject": "VTT Enduro Orbea Rallon M10 2023",
            "body": "...",
            "price": 4200,
            "url": "https://www.leboncoin.fr/...",
            "city": "Bayonne",
            "attributes": {"bicycle_wheel_size": "29\"", ...},
        },
        domain_hint="vtt_enduro",       # optionnel, depuis classify_vtt() en amont
        extract_model="llama3.2:3b",    # rapide pour identite + tri
        synth_model="mistral:7b",       # plus fort pour l'evaluation marche
    )
    # result["payload"] = dict Claude-compatible (drop-in pour update_enrichment)
    # result["meta"] = durations, identite, sources web, comparables LBC

Pipeline :
    1. extract_bike (Ollama) : identite (marque/modele/annee/roues/taille)
    2. enrich_identity : 2-3 recherches Jina, tri Ollama, extraction prix LLM via Jina Reader
    3. fetch_lbc_comparables : lib `lbc` -> annonces similaires actuelles -> mediane prix marche
    4. synthesize_evaluation : Ollama + regles de decote -> JSON Claude-compatible

Sortie payload (cle 'payload') :
    brand, model, year, frame_material, wheel_size, electric, size_label,
    vtt_category (xc/all_mountain/enduro/dh/dirt/null),
    condition_score, estimated_market_eur, deal_score,
    reasoning, pros[], cons[]

CLI :
    python enrich_bike.py --annonce <key> [--verbose --fetch-pages]    # legacy text
    python enrich_bike.py --ad-json ad.json [...]                       # ad LBC dict
    python enrich_bike.py --lbc-search "vtt enduro" --lbc-limit 3 [...] # fetch LBC live
"""

import argparse
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import random
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, quote_plus, unquote, urlparse
from urllib.request import Request, urlopen

import ollama

from benchmark_extraction import build_prompt, post_process, schema


def _load_env_file(path=".env"):
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file()
JINA_API_KEY = os.environ.get("JINA_API_KEY") or None


USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
        "Gecko/20100101 Firefox/124.0"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
]
PRICE_RE = re.compile(
    r"(?<!\d)(\d{1,3}(?:[ .]\d{3})*|\d{3,6})(?:[,.]\d{1,2})?\s*(?:\u20ac|eur|euros)(?![a-z])",
    flags=re.IGNORECASE,
)
MANUFACTURER_DOMAINS = {
    "bmc": "bmc-switzerland.com",
    "cannondale": "cannondale.com",
    "canyon": "canyon.com",
    "commencal": "commencal.com",
    "cube": "cube.eu",
    "decathlon": "decathlon.fr",
    "focus": "focus-bikes.com",
    "ghost": "ghost-bikes.com",
    "giant": "giant-bicycles.com",
    "haibike": "haibike.com",
    "ktm": "ktm-bikes.at",
    "megamo": "megamo.com",
    "mondraker": "mondraker.com",
    "orbea": "orbea.com",
    "rockrider": "decathlon.fr",
    "santa cruz": "santacruzbicycles.com",
    "scott": "scott-sports.com",
    "specialized": "specialized.com",
    "sunn": "sunn.fr",
    "trek": "trekbikes.com",
    "yt": "yt-industries.com",
}
PRICE_SOURCE_PROFILES = [
    {"name": "Velo Vert", "domain": "velovert.com", "priority": 20},
    {"name": "Big Bike Magazine", "domain": "bigbike-magazine.com", "priority": 30},
    {"name": "26in", "domain": "26in.fr", "priority": 40},
    {"name": "Pinkbike", "domain": "pinkbike.com", "priority": 50},
    {"name": "Bike Magazine", "domain": "bike-magazine.com", "priority": 60},
    {"name": "Vital MTB", "domain": "vitalmtb.com", "priority": 70},
    {"name": "99 Spokes", "domain": "99spokes.com", "priority": 80},
    {"name": "MTB Database", "domain": "mtbdatabase.com", "priority": 90},
    {"name": "Ekstere", "domain": "ekstere.eco", "priority": 100},
]
FUTURE_GEOMETRY_SOURCES = [
    {"name": "Geometry Geeks", "domain": "geometrygeeks.bike", "purpose": "geometry"},
]
EXCLUDED_RESULT_DOMAINS = (
    "leboncoin.fr",
    "leboncoin.com",
    "leboncoin.com.au",
)

CACHE_DIR = Path(".cache/enrich_bike")
CACHE_TTL_SECONDS = 7 * 24 * 3600
CACHE_ENABLED = True
DOMAIN_MIN_INTERVAL = {
    "duckduckgo.com": 8.0,
    "lite.duckduckgo.com": 8.0,
    "html.duckduckgo.com": 8.0,
    "bing.com": 6.0,
    "r.jina.ai": 0.3 if JINA_API_KEY else 3.0,
    "s.jina.ai": 0.6 if JINA_API_KEY else 99.0,
}
SAFE_URL_CHARS = ":/?#[]@!$&'()*+,;=%~"
LAST_REQUEST_TIME = {}


class DuckDuckGoLiteParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self._current_link = None
        self._current_text = []
        self._last_result = None
        self._capture_snippet = False
        self._snippet_text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            href = attrs["href"]
            class_name = attrs.get("class", "")
            if "result-link" in class_name or "/l/?" in href:
                self._current_link = href
                self._current_text = []
        elif tag in {"td", "span"}:
            class_name = attrs.get("class", "")
            if "result-snippet" in class_name:
                self._capture_snippet = True
                self._snippet_text = []

    def handle_data(self, data):
        if self._current_link is not None:
            self._current_text.append(data)
        elif self._capture_snippet:
            self._snippet_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._current_link is not None:
            title = normalize_space(" ".join(self._current_text))
            url = clean_duckduckgo_url(self._current_link)
            if title and url and not is_duckduckgo_internal(url):
                self._last_result = {"title": title, "url": url, "snippet": ""}
                self.results.append(self._last_result)
            self._current_link = None
            self._current_text = []
        elif tag in {"td", "span"} and self._capture_snippet:
            snippet = normalize_space(" ".join(self._snippet_text))
            if snippet and self._last_result and not self._last_result.get("snippet"):
                self._last_result["snippet"] = snippet
            self._capture_snippet = False
            self._snippet_text = []


class BingParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self._in_result = False
        self._result_depth = 0
        self._current = None
        self._capture_title = False
        self._title_text = []
        self._capture_snippet = False
        self._snippet_text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        class_name = attrs.get("class", "")

        if tag == "li" and "b_algo" in class_name:
            self._in_result = True
            self._result_depth = 1
            self._current = {"title": "", "url": "", "snippet": ""}
            return

        if not self._in_result:
            return

        self._result_depth += 1
        if tag == "a" and not self._current["url"] and attrs.get("href"):
            self._current["url"] = attrs["href"]
            self._capture_title = True
            self._title_text = []
        elif tag == "p" and not self._current["snippet"]:
            self._capture_snippet = True
            self._snippet_text = []

    def handle_data(self, data):
        if self._capture_title:
            self._title_text.append(data)
        elif self._capture_snippet:
            self._snippet_text.append(data)

    def handle_endtag(self, tag):
        if self._capture_title and tag == "a":
            self._current["title"] = normalize_space(" ".join(self._title_text))
            self._capture_title = False
            self._title_text = []
        elif self._capture_snippet and tag == "p":
            self._current["snippet"] = normalize_space(" ".join(self._snippet_text))
            self._capture_snippet = False
            self._snippet_text = []

        if self._in_result:
            self._result_depth -= 1
            if tag == "li" and self._result_depth <= 0:
                if self._current["title"] and self._current["url"]:
                    self.results.append(self._current)
                self._in_result = False
                self._current = None


def normalize_space(value):
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def random_user_agent():
    return random.choice(USER_AGENTS)


def build_headers(url, referer=None):
    user_agent = random_user_agent()
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.7,en;q=0.6",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
    }

    if referer:
        headers["Referer"] = referer
        ref_host = urlparse(referer).netloc.lower()
        url_host = urlparse(url).netloc.lower()
        headers["Sec-Fetch-Site"] = "same-origin" if ref_host == url_host else "cross-site"
    else:
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
            headers["Sec-Fetch-Site"] = "same-origin"
        else:
            headers["Sec-Fetch-Site"] = "none"

    if JINA_API_KEY:
        host = urlparse(url).netloc.lower()
        if host.endswith("r.jina.ai") or host.endswith("s.jina.ai"):
            headers["Authorization"] = f"Bearer {JINA_API_KEY}"

    if "Chrome" in user_agent and "Firefox" not in user_agent:
        headers["sec-ch-ua"] = (
            '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
        )
        headers["sec-ch-ua-mobile"] = "?0"
        if "Macintosh" in user_agent:
            headers["sec-ch-ua-platform"] = '"macOS"'
        elif "Linux" in user_agent:
            headers["sec-ch-ua-platform"] = '"Linux"'
        else:
            headers["sec-ch-ua-platform"] = '"Windows"'

    return headers


def cache_path(url):
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return CACHE_DIR / digest[:2] / f"{digest}.html"


def cache_read(url, max_age=CACHE_TTL_SECONDS):
    path = cache_path(url)
    if not path.exists():
        return None
    if time.time() - path.stat().st_mtime > max_age:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def cache_write(url, content):
    path = cache_path(url)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError:
        pass


def throttle_for_domain(url, default_min=0.3, default_max=0.8, verbose=False):
    domain = urlparse(url).netloc.lower().removeprefix("www.")
    min_interval = default_min
    for known, interval in DOMAIN_MIN_INTERVAL.items():
        if domain.endswith(known):
            min_interval = interval
            break

    elapsed = time.time() - LAST_REQUEST_TIME.get(domain, 0)
    if elapsed >= min_interval:
        wait = random.uniform(0.0, max(default_max - default_min, 0.1))
    else:
        wait = (min_interval - elapsed) + random.uniform(0.1, 0.4)

    if wait > 0:
        if verbose:
            print(f"[throttle] {domain} sleep {wait:.2f}s (interval {min_interval}s, since last {elapsed:.1f}s)")
        time.sleep(wait)
    LAST_REQUEST_TIME[domain] = time.time()


def safe_url(url):
    return quote(url, safe=SAFE_URL_CHARS)


def http_get(
    url,
    timeout=10,
    delay_min=0,
    delay_max=0,
    retries=0,
    verbose=False,
    referer=None,
    use_cache=None,
):
    url = safe_url(url)
    if use_cache is None:
        use_cache = CACHE_ENABLED
    if use_cache:
        cached = cache_read(url)
        if cached is not None:
            if verbose:
                print(f"[cache:hit] {url}")
            return cached

    last_error = None
    for attempt in range(retries + 1):
        throttle_for_domain(
            url,
            default_min=delay_min,
            default_max=delay_max,
            verbose=verbose,
        )
        request = Request(url, headers=build_headers(url, referer=referer))
        try:
            with urlopen(request, timeout=timeout) as response:
                content_type = response.headers.get("content-type", "")
                raw = response.read()
            break
        except HTTPError as exc:
            last_error = exc
            if exc.code not in {403, 429} or attempt >= retries:
                raise
            backoff = min(30 * (2 ** attempt), 300)
            backoff += random.uniform(0, backoff * 0.3)
            if verbose:
                print(
                    f"[http:retry] {exc.code} on {url} -> wait {backoff:.1f}s, "
                    f"retry {attempt + 1}/{retries}"
                )
            time.sleep(backoff)
    else:
        raise last_error

    encoding_match = re.search(r"charset=([^;\s]+)", content_type)
    encoding = encoding_match.group(1) if encoding_match else "utf-8"
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode(encoding, errors="replace")

    if use_cache:
        cache_write(url, content)
    return content


def clean_duckduckgo_url(url):
    url = unescape(url)
    if url.startswith("//"):
        url = "https:" + url

    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [None])[0]
        return unquote(target) if target else None

    return url


def is_duckduckgo_internal(url):
    parsed = urlparse(url)
    return parsed.netloc.endswith("duckduckgo.com")


def duckduckgo_search(query, max_results=8, timeout=10, delay_min=0, delay_max=0, retries=0, verbose=False):
    url = f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}"
    if verbose:
        print(f"[visit:search] duckduckgo | {url}")
    html = http_get(
        url,
        timeout=timeout,
        delay_min=delay_min,
        delay_max=delay_max,
        retries=retries,
        verbose=verbose,
        referer="https://duckduckgo.com/",
    )
    if "anomaly-modal" in html or "anomaly" in html[:20000].lower():
        return []
    parser = DuckDuckGoLiteParser()
    parser.feed(html)
    return unique_results(parser.results, max_results)


def bing_search(query, max_results=8, timeout=10, delay_min=0, delay_max=0, retries=0, verbose=False):
    url = f"https://www.bing.com/search?q={quote_plus(query)}"
    if verbose:
        print(f"[visit:search] bing | {url}")
    html = http_get(
        url,
        timeout=timeout,
        delay_min=delay_min,
        delay_max=delay_max,
        retries=retries,
        verbose=verbose,
        referer="https://www.bing.com/",
    )
    parser = BingParser()
    parser.feed(html)
    return unique_results(parser.results, max_results)


def jina_duckduckgo_search(query, max_results=8, timeout=10, delay_min=0, delay_max=0, retries=0, verbose=False):
    url = f"https://r.jina.ai/https://duckduckgo.com/html/?q={quote_plus(query)}"
    if verbose:
        print(f"[visit:search] duckduckgo+jina | {url}")
    markdown = http_get(
        url,
        timeout=timeout,
        delay_min=delay_min,
        delay_max=delay_max,
        retries=retries,
        verbose=verbose,
        referer="https://r.jina.ai/",
    )
    results = []

    matches = list(re.finditer(r"^## \[([^\]]+)\]\(([^)]+)\)", markdown, flags=re.MULTILINE))
    for index, match in enumerate(matches):
        title = normalize_space(match.group(1))
        url = clean_duckduckgo_url(match.group(2))
        if not title or not url or is_duckduckgo_internal(url):
            continue

        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        body = markdown[match.end() : next_start]
        body = re.sub(r"\[[^\]]*\]\([^)]+\)", " ", body)
        snippet = normalize_space(body)
        results.append({"title": title, "url": url, "snippet": snippet[:500]})

    return unique_results(results, max_results)


JINA_SEARCH_RESULT_RE = re.compile(
    r"\[(\d+)\]\s*Title:\s*(.+?)\n"
    r"\[\1\]\s*URL Source:\s*(\S+)\n"
    r"(?:\[\1\]\s*Description:\s*(.+?)\n)?",
    flags=re.DOTALL,
)


def jina_search(query, max_results=8, timeout=10, delay_min=0, delay_max=0, retries=0, verbose=False):
    url = f"https://s.jina.ai/?q={quote_plus(query)}"
    if verbose:
        print(f"[visit:search] jina | {url}")
    markdown = http_get(
        url,
        timeout=timeout,
        delay_min=delay_min,
        delay_max=delay_max,
        retries=retries,
        verbose=verbose,
        referer="https://jina.ai/",
    )

    results = []
    for match in JINA_SEARCH_RESULT_RE.finditer(markdown):
        title = normalize_space(match.group(2))
        target_url = (match.group(3) or "").strip()
        snippet = normalize_space(match.group(4) or "")
        if not title or not target_url:
            continue
        results.append({"title": title, "url": target_url, "snippet": snippet[:500]})

    if not results:
        for match in re.finditer(r"^##\s*\[([^\]]+)\]\(([^)]+)\)", markdown, flags=re.MULTILINE):
            title = normalize_space(match.group(1))
            target_url = match.group(2).strip()
            if title and target_url:
                results.append({"title": title, "url": target_url, "snippet": ""})

    return unique_results(results, max_results)


def web_search(query, max_results=8, timeout=10, delay_min=0, delay_max=0, retries=0, verbose=False):
    if JINA_API_KEY:
        backends = [
            ("jina", jina_search),
            ("duckduckgo+jina", jina_duckduckgo_search),
            ("duckduckgo", duckduckgo_search),
            ("bing", bing_search),
        ]
    else:
        backends = [
            ("duckduckgo", duckduckgo_search),
            ("duckduckgo+jina", jina_duckduckgo_search),
            ("bing", bing_search),
        ]
    last_error = None

    for engine, search_func in backends:
        try:
            results = search_func(
                query,
                max_results=max_results,
                timeout=timeout,
                delay_min=delay_min,
                delay_max=delay_max,
                retries=retries,
                verbose=verbose,
            )
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
            last_error = exc
            if verbose:
                print(f"[search:fallback] {engine} -> {exc}")
            continue

        if results:
            return results, engine

    if last_error:
        raise last_error
    return [], backends[-1][0]


def unique_results(results, max_results):
    unique = []
    seen = set()

    for result in results:
        if result["url"] in seen:
            continue
        seen.add(result["url"])
        unique.append(result)
        if len(unique) >= max_results:
            break
    return unique


def extract_prices(text):
    prices = []
    seen = set()
    for match in PRICE_RE.finditer(text or ""):
        raw = normalize_space(match.group(0))
        amount = parse_price_amount(raw)
        if amount is None:
            continue
        if amount < 50 or amount > 20000:
            continue
        key = (amount, raw.lower())
        if key in seen:
            continue
        seen.add(key)
        prices.append({"amount_eur": amount, "raw": raw})
    return prices


def parse_price_amount(raw):
    value = re.sub(r"(?i)\s*(\u20ac|eur|euros)\s*$", "", raw).strip()
    value = value.replace(" ", "")

    if re.search(r"[,.]\d{1,2}$", value):
        value = re.split(r"[,.]", value)[0]

    value = value.replace(".", "")
    if not value.isdigit():
        return None
    return int(value)


def html_to_text(html):
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    return normalize_space(html)


def fetch_page_text(url, timeout=10, delay_min=0, delay_max=0, retries=0, verbose=False):
    jina_url = f"https://r.jina.ai/{url}"
    if verbose:
        print(f"[fetch:jina] {url}")
    try:
        text = http_get(
            jina_url,
            timeout=timeout,
            delay_min=delay_min,
            delay_max=delay_max,
            retries=retries,
            verbose=verbose,
        )
        return {"ok": True, "error": None, "text": text[:250000], "via": "jina"}
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
        if verbose:
            print(f"[fetch:jina:fail] {exc} -> fallback direct")

    try:
        html = http_get(
            url,
            timeout=timeout,
            delay_min=delay_min,
            delay_max=delay_max,
            retries=retries,
            verbose=verbose,
        )
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc), "text": "", "via": "direct"}

    text = html_to_text(html)[:250000]
    return {"ok": True, "error": None, "text": text, "via": "direct"}


PRICE_CONTEXT_RE = re.compile(r"(?i)(?:€|\beur\b|\beuros?\b)")


def extract_price_context(text, window=400, max_chunks=8):
    if not text:
        return ""
    chunks = []
    last_end = -1
    for match in PRICE_CONTEXT_RE.finditer(text):
        start = max(0, match.start() - window)
        end = min(len(text), match.end() + window // 2)
        if start <= last_end:
            continue
        chunks.append(text[start:end])
        last_end = end
        if len(chunks) >= max_chunks:
            break
    if not chunks:
        return text[:4000]
    return "\n---\n".join(chunks)


def format_prices(prices):
    if not prices:
        return "aucun"
    parts = []
    for price in prices:
        amount = price["amount_eur"]
        kind = price.get("kind") or price.get("raw") or "?"
        parts.append(f"{amount} EUR ({kind})")
    return ", ".join(parts)


def extract_bike(model, annonce, timeout, verbose=False):
    start = time.time()
    if verbose:
        print(f"[extract] Ollama model={model}")
    client = ollama.Client(timeout=timeout)
    response = client.chat(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "Tu es un extracteur d'informations. Tu reponds uniquement en JSON strict.",
            },
            {"role": "user", "content": build_prompt(annonce)},
        ],
        format=schema,
        options={"temperature": 0},
    )
    data = json.loads(response["message"]["content"])
    identity = post_process(data, annonce)
    if verbose:
        print(f"[extract] identity={json.dumps(identity, ensure_ascii=False)}")
    return identity, time.time() - start


def compact_identity(identity):
    parts = [
        identity.get("marque"),
        identity.get("modele"),
        identity.get("version"),
        str(identity.get("annee")) if identity.get("annee") else None,
    ]
    return " ".join(part for part in parts if part)


def wheel_size_inches(identity):
    raw = str(identity.get("taille_roues") or "").lower()
    match = re.search(r"(\d{2}(?:[.,]\d)?)", raw)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def is_junior_bike(identity):
    size = wheel_size_inches(identity)
    return size is not None and 14 <= size <= 24


def bike_description(identity):
    lines = []
    if identity.get("marque"):
        lines.append(f"Marque: {identity['marque']}")
    if identity.get("modele"):
        lines.append(f"Modele: {identity['modele']}")
    if identity.get("version"):
        lines.append(f"Version: {identity['version']}")
    if identity.get("annee"):
        lines.append(f"Annee: {identity['annee']}")
    if identity.get("taille_roues"):
        lines.append(f"Taille de roues: {identity['taille_roues']}")
    if identity.get("taille_cadre"):
        lines.append(f"Taille du cadre: {identity['taille_cadre']}")
    if is_junior_bike(identity):
        lines.append("Categorie: velo junior/enfant (roues 14-24 pouces)")
    return "\n".join(lines) if lines else "(velo non identifie)"


def search_query_suffix(identity):
    size = wheel_size_inches(identity)
    if size is None:
        return ""
    label = int(size) if size.is_integer() else size
    base = f' "{label} pouces"'
    if 14 <= size <= 24:
        base += " junior enfant"
    return base


def get_manufacturer_domain(identity):
    brand = normalize_space(identity.get("marque") or "").lower()
    if not brand:
        return None

    brand_parts = [part.strip() for part in re.split(r"[/,]", brand) if part.strip()]
    brand_parts.append(brand)
    for candidate in brand_parts:
        if candidate in MANUFACTURER_DOMAINS:
            return MANUFACTURER_DOMAINS[candidate]
    return None


def source_profile_for_url(url, identity=None):
    parsed = urlparse(url)
    domain = parsed.netloc.lower().removeprefix("www.")

    manufacturer_domain = get_manufacturer_domain(identity or {})
    if manufacturer_domain and domain.endswith(manufacturer_domain):
        return {"name": "Constructeur", "domain": manufacturer_domain, "priority": 10}

    for profile in PRICE_SOURCE_PROFILES:
        if domain.endswith(profile["domain"]):
            return profile

    return {"name": "Autre", "domain": domain, "priority": 999}


def build_search_queries(identity):
    base = compact_identity(identity)
    if not base:
        return []

    suffix = search_query_suffix(identity)
    queries = [
        {"source": "Web general", "domain": None, "query": f"{base}{suffix} prix fiche technique"},
        {"source": "Web general", "domain": None, "query": f"{base}{suffix} test review velo"},
    ]

    manufacturer_domain = get_manufacturer_domain(identity)
    if manufacturer_domain:
        queries.insert(
            0,
            {
                "source": "Constructeur",
                "domain": manufacturer_domain,
                "query": f"{base}{suffix} site:{manufacturer_domain}",
            },
        )
    return queries


RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "selected": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["i", "reason"],
            },
        }
    },
    "required": ["selected"],
}


def rank_sources_with_llm(model, identity, candidates, top_k=8, timeout=25, verbose=False):
    if not candidates:
        return []

    bike_desc = bike_description(identity)
    junior_warning = (
        "ATTENTION: il s'agit d'un velo JUNIOR/ENFANT. Rejette toute page qui parle "
        "de la version adulte du meme modele (roues 26\", 27.5\", 29\"). Garde uniquement "
        "les pages qui mentionnent explicitement la taille de roues correspondante.\n"
        if is_junior_bike(identity)
        else ""
    )
    payload = [
        {
            "i": index,
            "title": (candidate.get("title") or "")[:200],
            "url": candidate["url"],
            "snippet": (candidate.get("snippet") or "")[:300],
            "source": candidate.get("source_name"),
        }
        for index, candidate in enumerate(candidates)
    ]

    prompt = (
        f"Velo cible:\n{bike_desc}\n\n"
        f"{junior_warning}"
        f"Voici une liste de resultats web (titre, url, extrait, source). "
        f"Selectionne UNIQUEMENT ceux qui correspondent EXACTEMENT a ce velo "
        f"(meme marque, meme modele, ET meme taille de roues — un velo 24 pouces "
        f"n'est PAS le meme produit qu'un 27.5 ou un 29 pouces). "
        f"Ils doivent contenir le PRIX NEUF, la FICHE TECHNIQUE ou la GEOMETRIE. "
        f"Ignore: autres tailles de roues, autres modeles, vetements/accessoires, "
        f"forums sans info technique, resultats hors sujet. "
        f"Limite a {top_k} resultats max, par ordre de pertinence decroissante. "
        f"Pour chaque resultat retenu, fournis i (l'index entier) et une raison courte.\n\n"
        f"Resultats:\n{json.dumps(payload, ensure_ascii=False)}"
    )

    if verbose:
        print(f"[rank] Ollama model={model}, candidats={len(candidates)}")

    client = ollama.Client(timeout=timeout)
    response = client.chat(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "Tu tries des resultats web pour identifier les sources techniques et de prix d'un velo. Tu reponds uniquement en JSON strict.",
            },
            {"role": "user", "content": prompt},
        ],
        format=RANK_SCHEMA,
        options={"temperature": 0},
    )

    data = json.loads(response["message"]["content"])
    selected = []
    seen_idx = set()
    for item in data.get("selected", []):
        idx = item.get("i")
        if not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
            continue
        if idx in seen_idx:
            continue
        seen_idx.add(idx)
        selected.append({**candidates[idx], "llm_reason": item.get("reason", "")})
        if len(selected) >= top_k:
            break

    if verbose:
        for entry in selected:
            print(f"[rank:keep] {entry['url']} — {(entry.get('llm_reason') or '')[:80]}")

    return selected


PRICE_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "prices": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "amount_eur": {"type": "integer"},
                    "kind": {
                        "type": "string",
                        "enum": ["msrp", "current", "used", "sale", "unknown"],
                    },
                    "context": {"type": "string"},
                },
                "required": ["amount_eur", "kind"],
            },
        }
    },
    "required": ["prices"],
}


def extract_prices_with_llm(model, identity, page_text, source_url, timeout=25, verbose=False):
    if not page_text:
        return []

    bike_desc = bike_description(identity)
    junior_warning = (
        "ATTENTION: il s'agit d'un velo JUNIOR/ENFANT. Ignore les prix qui se "
        "rapportent a la version adulte du meme modele (roues 26\", 27.5\", 29\"). "
        "Ne retiens que les prix qui mentionnent explicitement la bonne taille de roues, "
        "ou qui sont sans ambiguite sur la version junior.\n"
        if is_junior_bike(identity)
        else ""
    )
    excerpt = extract_price_context(page_text)

    prompt = (
        f"Velo cible:\n{bike_desc}\n"
        f"Source: {source_url}\n\n"
        f"{junior_warning}"
        f"Voici des extraits d'une page web autour de mentions de prix. "
        f"Identifie UNIQUEMENT les prix qui correspondent au velo cible "
        f"(meme marque, meme modele, MEME taille de roues — pas les composants seuls, "
        f"pas d'autres tailles/modeles, pas les accessoires). "
        f"Pour chaque prix retenu:\n"
        f"- amount_eur: montant entier en euros\n"
        f"- kind: 'msrp' (prix catalogue/RRP/barre), 'current' (prix de vente neuf actuel), "
        f"'used' (occasion), 'sale' (promotion explicite), 'unknown' si ambigu\n"
        f"- context: courte phrase tiree de l'extrait justifiant le classement\n\n"
        f"Si la page ne concerne pas ce velo specifique, retourne une liste vide.\n\n"
        f"Extraits:\n{excerpt}"
    )

    if verbose:
        print(f"[price:llm] {source_url} (chars={len(excerpt)})")

    client = ollama.Client(timeout=timeout)
    response = client.chat(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "Tu extrais les prix d'un velo specifique a partir d'une page web. Tu reponds uniquement en JSON strict.",
            },
            {"role": "user", "content": prompt},
        ],
        format=PRICE_EXTRACTION_SCHEMA,
        options={"temperature": 0},
    )

    data = json.loads(response["message"]["content"])
    prices = []
    seen = set()
    for item in data.get("prices", []):
        amount = item.get("amount_eur")
        if not isinstance(amount, int) or amount < 50 or amount > 50000:
            continue
        kind = item.get("kind") or "unknown"
        if kind not in {"msrp", "current", "used", "sale", "unknown"}:
            kind = "unknown"
        key = (amount, kind)
        if key in seen:
            continue
        seen.add(key)
        prices.append(
            {
                "amount_eur": amount,
                "kind": kind,
                "context": (item.get("context") or "")[:300],
            }
        )

    if verbose:
        for entry in prices:
            print(
                f"[price:llm:{entry['kind']}] {entry['amount_eur']} EUR — "
                f"{entry['context'][:80]}"
            )

    return prices


def enrich_identity(
    identity,
    model,
    max_results,
    fetch_pages,
    http_timeout,
    ollama_timeout,
    delay_min,
    delay_max,
    retries,
    top_sources=8,
    verbose=False,
):
    query_specs = build_search_queries(identity)
    search_runs = []
    candidates = []
    seen_urls = set()

    for query_spec in query_specs:
        query = query_spec["query"]
        if verbose:
            source = query_spec.get("source") or "source inconnue"
            domain = query_spec.get("domain") or "web"
            print(f"[search] {source} ({domain}) -> {query}")
        try:
            results, search_engine = web_search(
                query,
                max_results=max_results,
                timeout=http_timeout,
                delay_min=delay_min,
                delay_max=delay_max,
                retries=retries,
                verbose=verbose,
            )
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
            if verbose:
                print(f"[search:error] {query} -> {exc}")
            search_runs.append(
                {**query_spec, "search_engine": None, "error": str(exc), "results_count": 0}
            )
            continue

        if verbose:
            print(f"[search] engine={search_engine}, results={len(results)}")

        new_count = 0
        for result in results:
            if result["url"] in seen_urls:
                continue
            netloc = urlparse(result["url"]).netloc.lower().removeprefix("www.")
            if any(netloc.endswith(d) for d in EXCLUDED_RESULT_DOMAINS):
                if verbose:
                    print(f"[skip:lbc] {result['url']}")
                continue
            seen_urls.add(result["url"])
            source_profile = source_profile_for_url(result["url"], identity)
            candidates.append(
                {
                    **result,
                    "from_query": query,
                    "from_source": query_spec.get("source"),
                    "source_name": source_profile["name"],
                    "source_domain": source_profile["domain"],
                    "source_priority": source_profile["priority"],
                }
            )
            new_count += 1

        search_runs.append(
            {
                **query_spec,
                "search_engine": search_engine,
                "error": None,
                "results_count": new_count,
            }
        )

    rank_method = "llm"
    try:
        selected = rank_sources_with_llm(
            model, identity, candidates, top_k=top_sources, timeout=ollama_timeout, verbose=verbose
        )
    except Exception as exc:
        if verbose:
            print(f"[rank:error] {exc} -> fallback sur priorite par domaine")
        rank_method = "fallback_priority"
        selected = sorted(candidates, key=lambda r: r["source_priority"])[:top_sources]

    enriched_results = []
    for result in selected:
        combined_text = f'{result["title"]} {result.get("snippet", "")}'
        snippet_prices = [
            {"amount_eur": p["amount_eur"], "kind": "unknown", "context": p["raw"]}
            for p in extract_prices(combined_text)
        ]
        enriched = {
            **result,
            "prices_in_result": snippet_prices,
        }
        if verbose:
            print(f"[result] {result['source_name']} | {result['title']} | {result['url']}")
            print(f"[price:result] {format_prices(enriched['prices_in_result'])}")
        if fetch_pages:
            if verbose:
                print(f"[fetch] {result['url']}")
            page = fetch_page_text(
                result["url"],
                timeout=http_timeout,
                delay_min=delay_min,
                delay_max=delay_max,
                retries=retries,
                verbose=verbose,
            )
            enriched["page_fetch_ok"] = page["ok"]
            enriched["page_fetch_error"] = page["error"]
            enriched["page_fetch_via"] = page.get("via")

            page_prices = []
            if page["ok"]:
                try:
                    page_prices = extract_prices_with_llm(
                        model,
                        identity,
                        page["text"],
                        result["url"],
                        timeout=ollama_timeout,
                        verbose=verbose,
                    )
                except Exception as exc:
                    if verbose:
                        print(f"[price:llm:error] {exc} -> fallback regex")
                    page_prices = [
                        {"amount_eur": p["amount_eur"], "kind": "unknown", "context": p["raw"]}
                        for p in extract_prices(page["text"])
                    ][:10]
                if verbose and not page_prices:
                    print("[price:page] aucun prix retenu par Ollama")
            elif verbose:
                print(f"[fetch:error] {page['error']}")

            enriched["prices_in_page"] = page_prices[:10]
        enriched_results.append(enriched)

    return {
        "queries": [query_spec["query"] for query_spec in query_specs],
        "query_specs": query_specs,
        "request_policy": {
            "delay_min": delay_min,
            "delay_max": delay_max,
            "retries": retries,
            "user_agent_rotation": len(USER_AGENTS),
        },
        "price_sources": {
            "manufacturer_domain": get_manufacturer_domain(identity),
            "catalogue_sources": PRICE_SOURCE_PROFILES,
            "future_geometry_sources": FUTURE_GEOMETRY_SOURCES,
        },
        "search_runs": search_runs,
        "candidates_count": len(candidates),
        "rank_method": rank_method,
        "selected_results": enriched_results,
        "price_summary": summarize_prices(enriched_results),
    }


DECOTE_RULES_BIKE = """
DECOTE VTT/velo occasion (% du prix neuf catalogue):
- < 4 ans : 50-70%
- 4-7 ans : 25-40%
- 8-12 ans : 12-22%  (obsolescence techno)
- > 12 ans : 5-15%

VELOS JUNIOR (roues 14-24 pouces) : decote moins forte (usage moins agressif)
- < 4 ans : 60-80%
- 4-7 ans : 35-55%
- > 8 ans : 15-30%

PENALITES (cumulables):
- 26" sortie avant ~2015 : -30% (standard obsolete)
- axe 9mm / non-Boost : -20%
- cassette 9V/10V : -10%
- cadre alu raye / impact : -10 a -20%
- modele VTT < 2018 : ne PAS surestimer la cote

CLASSIFICATION vtt_category (mm de debattement, valeur enum):
- xc            : 100-120
- all_mountain  : 120-150  (= ancien "trail")
- enduro        : 150-170
- dh            : 180-200  (incl. freeride)
- dirt          : hardtail jump/pumptrack
- null          : non-VTT (route, gravel, ville, junior, BMX) ou impossible a trancher

condition_score (0-100):
- 0=HS / 30=tres use / 50=usure visible / 80=bon etat / 95+=quasi neuf

deal_score (0-100):
- 0=tres cher / 30=un peu cher / 50=au marche / 70=sous marche -15 a -30% / 90+=>-30%
- Si prix demande inconnu : deal_score = 50 (neutre)
- REGLE: ne PAS baisser le deal_score pour suspicion d'arnaque (les doutes vont dans cons).

PRIORITE pour estimated_market_eur:
1. Si comparables LBC fournis (ads similaires actuelles) : la mediane est le signal le plus fiable.
2. Sinon : MSRP catalogue * decote selon annee + penalites.
3. Sinon : fourchette prudente avec reasoning explicite sur l'incertitude.

frame_material : "carbon", "aluminium", "acier", "titane" ou null.
wheel_size : "12", "14", "16", "18", "20", "24", "26", "27.5", "28", "29" ou null.
electric : true si VAE/ebike, false sinon, null si inconnu.
size_label : taille cadre XS/S/M/L/XL/XXL ou null.
"""


SYNTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "brand": {"type": ["string", "null"]},
        "model": {"type": ["string", "null"]},
        "year": {"type": ["integer", "null"]},
        "frame_material": {"type": ["string", "null"]},
        "wheel_size": {"type": ["string", "null"]},
        "electric": {"type": ["boolean", "null"]},
        "size_label": {"type": ["string", "null"]},
        "vtt_category": {
            "type": ["string", "null"],
            "enum": ["xc", "all_mountain", "enduro", "dh", "dirt", None],
        },
        "condition_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "estimated_market_eur": {"type": "number", "minimum": 0},
        "deal_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "reasoning": {"type": "string", "maxLength": 500},
        "pros": {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 4},
        "cons": {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 4},
    },
    "required": [
        "brand", "model", "year", "electric",
        "condition_score", "estimated_market_eur", "deal_score",
        "reasoning", "pros", "cons",
    ],
    "additionalProperties": False,
}


def extract_asking_price(annonce):
    text = annonce if isinstance(annonce, str) else (
        f"{annonce.get('subject', '')} {annonce.get('body', '')}"
    )
    prices = extract_prices(text)
    candidates = [p["amount_eur"] for p in prices if 50 <= p["amount_eur"] <= 30000]
    if not candidates:
        return None
    return max(candidates)


def build_synthesis_prompt(annonce, identity, price_summary, asking_price, lbc_comparables=None, domain_hint=None):
    text = annonce if isinstance(annonce, str) else (
        f"{annonce.get('subject', '')}\n\n{annonce.get('body', '')}"
    )
    if len(text) > 2500:
        text = text[:2500] + "..."

    estimate = (price_summary or {}).get("estimate") or {}
    msrp = estimate.get("msrp_eur")
    used_market_web = estimate.get("used_eur")

    by_kind = (price_summary or {}).get("by_kind") or {}
    web_samples = []
    for kind in ("msrp", "current", "used", "sale"):
        for p in (by_kind.get(kind) or [])[:3]:
            web_samples.append(f"  - {kind}: {p['amount_eur']} EUR ({p.get('source_name', '?')})")
    web_samples_block = "\n".join(web_samples) or "  (aucun)"

    lbc_comparables = lbc_comparables or []
    lbc_prices = [c["price_eur"] for c in lbc_comparables if c.get("price_eur")]
    lbc_block = "  (aucun comparable LBC)"
    lbc_median = None
    if lbc_prices:
        sorted_prices = sorted(lbc_prices)
        n = len(sorted_prices)
        lbc_median = sorted_prices[n // 2] if n % 2 else (sorted_prices[n // 2 - 1] + sorted_prices[n // 2]) / 2
        lbc_block_lines = [
            f"  - mediane: {int(lbc_median)} EUR sur {n} ads",
            f"  - min/max: {int(min(lbc_prices))} / {int(max(lbc_prices))} EUR",
            "  - echantillons:",
        ]
        for c in lbc_comparables[:6]:
            lbc_block_lines.append(f"    * {c.get('price_eur', '?')} EUR — {(c.get('subject') or '')[:80]}")
        lbc_block = "\n".join(lbc_block_lines)

    domain_line = f"\nDomaine indique par l'amont : {domain_hint}\n" if domain_hint else ""

    return f"""Annonce :
{text}

Identite extraite (extracteur Ollama, peut etre incomplete) :
{json.dumps(identity, ensure_ascii=False, indent=2)}
{domain_line}
Prix demande dans l'annonce : {f'{asking_price} EUR' if asking_price else 'inconnu'}

Comparables LBC actuels (ads similaires, le PLUS FIABLE pour le marche occasion) :
{lbc_block}

Resultats catalogue (recherche web) :
- MSRP estime (mediane msrp+current) : {msrp if msrp else 'inconnu'} EUR
- Prix occasion web (mediane used+sale) : {used_market_web if used_market_web else 'inconnu'} EUR
- Echantillons :
{web_samples_block}

Tache : remplis TOUS les champs du schema.
1. brand/model/year : copie/corrige depuis l'identite. Si l'extracteur s'est trompe et que tu vois mieux dans l'annonce, corrige.
2. frame_material, wheel_size, electric, size_label : extrait depuis l'annonce/identite. null si vraiment inconnu.
3. vtt_category : enum (xc/all_mountain/enduro/dh/dirt) seulement si VTT, sinon null.
4. estimated_market_eur : utilise EN PRIORITE la mediane LBC si dispo. Sinon MSRP * decote.
5. condition_score (0-100) : depuis le texte ("tres bon etat" ~80, "neuf" 95+, etc.).
6. deal_score (0-100) : ecart prix_demande vs estimated_market_eur. 50 si prix inconnu.
7. reasoning : 2-3 phrases (~80 mots max) expliquant le score + situant le modele.
8. pros : 2-4 bullets concis (max ~10 mots/item).
9. cons : 2-4 bullets concis (max ~10 mots/item).

Sois rigoureux. Si l'identite est lacunaire, dis-le dans reasoning et donne une fourchette prudente.
"""


def synthesize_evaluation(
    model,
    annonce,
    identity,
    price_summary,
    asking_price=None,
    lbc_comparables=None,
    domain_hint=None,
    timeout=60,
    verbose=False,
):
    if verbose:
        print(f"[synth] Ollama model={model}")
    started = time.time()
    client = ollama.Client(timeout=timeout)
    response = client.chat(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "Tu es un expert du marche francais d'occasion (velos, VTT, ebikes, junior, route, gravel, BMX). "
                    "Tu evalues une annonce a partir du texte, de l'identite extraite, "
                    "des prix catalogue trouves sur le web, et des annonces LBC similaires. "
                    "Tu reponds UNIQUEMENT en JSON strict respectant le schema fourni.\n"
                    + DECOTE_RULES_BIKE
                ),
            },
            {
                "role": "user",
                "content": build_synthesis_prompt(
                    annonce, identity, price_summary, asking_price,
                    lbc_comparables=lbc_comparables, domain_hint=domain_hint,
                ),
            },
        ],
        format=SYNTHESIS_SCHEMA,
        options={"temperature": 0},
    )
    data = json.loads(response["message"]["content"])
    duration = time.time() - started
    if verbose:
        print(
            f"[synth] brand={data.get('brand')} model={data.get('model')} "
            f"vtt_cat={data.get('vtt_category')} cond={data.get('condition_score')} "
            f"market={data.get('estimated_market_eur')}EUR deal={data.get('deal_score')} "
            f"({duration:.2f}s)"
        )
    return data, duration


LBC_ATTR_SKIP_KEYS = {
    "profile_picture_url", "rating_score", "rating_count", "is_bundleable",
    "purchase_cta_visible", "negotiation_cta_visible", "country_isocode3166",
    "shipping_type", "shippable", "is_import", "vehicle_available_payment_methods",
    "vehicle_is_eligible_p2p", "estimated_parcel_size", "estimated_parcel_weight",
    "payment_methods", "stock_quantity", "activity_sector", "argus_object_id",
    "spare_parts_availability", "bicycode",
}


def render_lbc_ad(ad):
    if isinstance(ad, str):
        return ad
    parts = []
    if ad.get("subject"):
        parts.append(f"Titre original\n{ad['subject']}")
    body = ad.get("body")
    if body:
        if len(body) > 1500:
            body = body[:1500] + "..."
        parts.append(f"Description complete\n{body}")
    attrs = ad.get("attributes") or {}
    attr_lines = "\n".join(
        f"  - {k}: {v}" for k, v in sorted(attrs.items())
        if k not in LBC_ATTR_SKIP_KEYS and v
    )
    if attr_lines:
        parts.append(f"Attributs Leboncoin\n{attr_lines}")
    if ad.get("price"):
        parts.append(f"Prix: {ad['price']} EUR")
    if ad.get("city"):
        parts.append(f"Ville: {ad['city']}")
    return "\n\n".join(parts)


def fetch_lbc_comparables(identity, limit=15, exclude_ad_id=None, verbose=False):
    if not identity.get("marque") or not identity.get("modele"):
        if verbose:
            print("[lbc] identity incomplete, skip comparables")
        return []
    try:
        import lbc
    except ImportError:
        if verbose:
            print("[lbc] lib `lbc` not installed, skip comparables")
        return []

    parts = [identity["marque"], identity["modele"]]
    if identity.get("annee"):
        parts.append(str(identity["annee"]))
    query = " ".join(parts)
    if verbose:
        print(f"[lbc:search] {query} (cat=LOISIRS_VELOS, limit={limit})")

    try:
        client = lbc.Client()
        result = client.search(
            text=query,
            category=lbc.Category.LOISIRS_VELOS,
            limit=limit,
            sort=lbc.Sort.NEWEST,
        )
    except Exception as exc:
        if verbose:
            print(f"[lbc:error] {exc}")
        return []

    comparables = []
    wheel_target = identity.get("taille_roues")
    for raw_ad in (result.ads or []):
        if exclude_ad_id is not None and raw_ad.id == exclude_ad_id:
            continue
        if raw_ad.price is None or raw_ad.price <= 50:
            continue
        if raw_ad.price > 30000:
            continue
        attrs = {}
        for a in (raw_ad.attributes or []):
            if a.key and a.value_label:
                attrs[a.key] = a.value_label
        if wheel_target:
            ad_wheel = attrs.get("bicycle_wheel_size", "")
            if ad_wheel and wheel_target not in str(ad_wheel):
                continue
        loc = raw_ad.location
        comparables.append({
            "id": raw_ad.id,
            "subject": raw_ad.subject,
            "price_eur": float(raw_ad.price),
            "url": raw_ad.url,
            "city": loc.city_label if loc else None,
            "posted_at": raw_ad.first_publication_date,
        })

    if verbose:
        print(f"[lbc:found] {len(comparables)} comparables retenus")
    return comparables


def fetch_lbc_ad(query, limit=1, verbose=False):
    try:
        import lbc
    except ImportError:
        raise RuntimeError("lib `lbc` non installee. Run: pip install lbc")
    if verbose:
        print(f"[lbc:search] {query} (cat=LOISIRS_VELOS, limit={limit})")
    client = lbc.Client()
    result = client.search(
        text=query,
        category=lbc.Category.LOISIRS_VELOS,
        limit=limit,
        sort=lbc.Sort.NEWEST,
    )
    ads = []
    for raw_ad in (result.ads or []):
        attrs = {}
        for a in (raw_ad.attributes or []):
            if a.key and a.value_label:
                attrs[a.key] = a.value_label
        loc = raw_ad.location
        ads.append({
            "id": raw_ad.id,
            "subject": raw_ad.subject,
            "body": raw_ad.body,
            "price": float(raw_ad.price) if raw_ad.price is not None else None,
            "url": raw_ad.url,
            "city": loc.city_label if loc else None,
            "zipcode": loc.zipcode if loc else None,
            "first_publication_date": raw_ad.first_publication_date,
            "category_id": raw_ad.category_id,
            "category_name": raw_ad.category_name,
            "attributes": attrs,
        })
    return ads


def enrich_ad(
    ad,
    domain_hint=None,
    extract_model="llama3.2:3b",
    synth_model="mistral:7b",
    fetch_pages=True,
    fetch_lbc=True,
    top_sources=8,
    max_results=6,
    http_timeout=10,
    ollama_timeout=25,
    synth_timeout=60,
    delay_min=0.3,
    delay_max=0.8,
    retries=2,
    use_cache=True,
    verbose=False,
):
    """Enrich an LBC bike ad and return Claude-compatible payload + meta.

    `ad` is a dict {id, subject, body, price, url, city, attributes}.
    Returns {"payload": <Claude-compatible dict>, "meta": <durations, sources>}.
    """
    global CACHE_ENABLED
    saved_cache = CACHE_ENABLED
    CACHE_ENABLED = use_cache

    started = time.time()
    annonce_text = render_lbc_ad(ad)
    asking_price = (ad.get("price") if isinstance(ad, dict) else None) or extract_asking_price(annonce_text)

    try:
        identity, ext_dur = extract_bike(extract_model, annonce_text, ollama_timeout, verbose=verbose)
    except Exception as exc:
        if verbose:
            print(f"[extract:error] {exc}")
        identity = {}
        ext_dur = 0.0

    web_started = time.time()
    try:
        web = enrich_identity(
            identity=identity,
            model=extract_model,
            max_results=max_results,
            fetch_pages=fetch_pages,
            http_timeout=http_timeout,
            ollama_timeout=ollama_timeout,
            delay_min=delay_min,
            delay_max=delay_max,
            retries=retries,
            top_sources=top_sources,
            verbose=verbose,
        )
    except Exception as exc:
        if verbose:
            print(f"[web:error] {exc}")
        web = {"price_summary": {"estimate": {}, "by_kind": {}}, "candidates_count": 0, "selected_results": []}
    web_dur = time.time() - web_started

    lbc_started = time.time()
    comparables = []
    exclude_id = ad.get("id") if isinstance(ad, dict) else None
    if fetch_lbc:
        try:
            comparables = fetch_lbc_comparables(
                identity, limit=15, exclude_ad_id=exclude_id, verbose=verbose,
            )
        except Exception as exc:
            if verbose:
                print(f"[lbc:error] {exc}")
            comparables = []
    lbc_dur = time.time() - lbc_started

    synth_dur = 0.0
    evaluation = None
    synth_error = None
    try:
        evaluation, synth_dur = synthesize_evaluation(
            model=synth_model,
            annonce=annonce_text,
            identity=identity,
            price_summary=web.get("price_summary"),
            asking_price=asking_price,
            lbc_comparables=comparables,
            domain_hint=domain_hint,
            timeout=synth_timeout,
            verbose=verbose,
        )
    except Exception as exc:
        if verbose:
            print(f"[synth:error] {exc}")
        synth_error = str(exc)

    CACHE_ENABLED = saved_cache

    if evaluation is None:
        wheel_size = identity.get("taille_roues")
        evaluation = {
            "brand": identity.get("marque"),
            "model": identity.get("modele"),
            "year": identity.get("annee"),
            "frame_material": None,
            "wheel_size": wheel_size,
            "electric": None,
            "size_label": identity.get("taille"),
            "vtt_category": None,
            "condition_score": 50,
            "estimated_market_eur": 0,
            "deal_score": 50,
            "reasoning": f"Synthese indisponible ({synth_error or 'timeout'}). Donnees identite/web seulement.",
            "pros": [],
            "cons": [],
        }

    lbc_prices = [c["price_eur"] for c in comparables if c.get("price_eur")]
    lbc_median = None
    if lbc_prices:
        s = sorted(lbc_prices)
        n = len(s)
        lbc_median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    total = time.time() - started
    return {
        "payload": evaluation,
        "meta": {
            "ad_id": ad.get("id") if isinstance(ad, dict) else None,
            "ad_url": ad.get("url") if isinstance(ad, dict) else None,
            "ad_subject": ad.get("subject") if isinstance(ad, dict) else None,
            "asking_price_eur": asking_price,
            "identity": identity,
            "web_summary": {
                "msrp_eur": (web.get("price_summary") or {}).get("estimate", {}).get("msrp_eur"),
                "used_eur_web": (web.get("price_summary") or {}).get("estimate", {}).get("used_eur"),
                "candidates_count": web.get("candidates_count"),
                "selected_count": len(web.get("selected_results") or []),
            },
            "lbc_comparables": {
                "count": len(comparables),
                "median_eur": lbc_median,
                "samples": comparables[:5],
            },
            "durations": {
                "extraction_s": round(ext_dur, 2),
                "web_s": round(web_dur, 2),
                "lbc_s": round(lbc_dur, 2),
                "synth_s": round(synth_dur, 2),
                "total_s": round(total, 2),
            },
            "models": {
                "extract": extract_model,
                "synth": synth_model,
            },
            "synth_error": synth_error,
        },
    }


def _median(values):
    cleaned = sorted(int(v) for v in values if isinstance(v, (int, float)) and v >= 500)
    if not cleaned:
        return None
    n = len(cleaned)
    if n % 2 == 1:
        return cleaned[n // 2]
    return (cleaned[n // 2 - 1] + cleaned[n // 2]) // 2


def summarize_prices(results):
    prices = []
    for result in results:
        for source_key in ("prices_in_result", "prices_in_page"):
            for price in result.get(source_key, []):
                prices.append(
                    {
                        "amount_eur": price["amount_eur"],
                        "kind": price.get("kind", "unknown"),
                        "context": price.get("context") or price.get("raw") or "",
                        "source": result["url"],
                        "source_title": result["title"],
                        "source_name": result.get("source_name", "Autre"),
                        "source_domain": result.get("source_domain"),
                        "source_priority": result.get("source_priority", 999),
                        "where": source_key,
                    }
                )

    if not prices:
        return {
            "count": 0,
            "by_kind": {"msrp": [], "current": [], "used": [], "sale": [], "unknown": []},
            "estimate": {"msrp_eur": None, "used_eur": None},
        }

    unique = []
    seen = set()
    for price in sorted(prices, key=lambda item: (item["source_priority"], item["amount_eur"])):
        key = (price["amount_eur"], price["kind"], price["source"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(price)

    by_kind = {"msrp": [], "current": [], "used": [], "sale": [], "unknown": []}
    for price in unique:
        kind = price["kind"] if price["kind"] in by_kind else "unknown"
        by_kind[kind].append(price)

    msrp_pool = [p["amount_eur"] for p in by_kind["msrp"] + by_kind["current"]]
    used_pool = [p["amount_eur"] for p in by_kind["used"] + by_kind["sale"]]

    return {
        "count": len(unique),
        "by_kind": {
            "msrp": by_kind["msrp"][:10],
            "current": by_kind["current"][:10],
            "used": by_kind["used"][:10],
            "sale": by_kind["sale"][:5],
            "unknown": by_kind["unknown"][:10],
        },
        "estimate": {
            "msrp_eur": _median(msrp_pool),
            "used_eur": _median(used_pool),
        },
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Agent d'identification, recherche web et evaluation marche pour velos.")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--annonce", help="Cle de l'annonce dans le fichier JSON local (mode test legacy).")
    src.add_argument("--ad-json", help="Chemin d'un JSON d'annonce LBC-style (id, subject, body, price, url, city, attributes).")
    src.add_argument("--lbc-search", help="Query Leboncoin pour fetch des annonces et les enrichir.")
    parser.add_argument("--annonces", default="data/annonces.json", help="Fichier JSON des annonces (mode --annonce).")
    parser.add_argument("--lbc-limit", type=int, default=3, help="Nombre d'annonces a fetcher en mode --lbc-search.")
    parser.add_argument("--no-lbc-comparables", action="store_true", help="Desactive la recherche d'annonces LBC similaires pour le marche.")
    parser.add_argument("--domain", help="Domaine indicatif (vtt_enduro, vtt_dh, etc.) passe a la synthese.")
    parser.add_argument("--model", default="llama3.2:3b", help="Modele Ollama utilise pour identifier le velo et trier les sources web.")
    parser.add_argument("--synth-model", default="mistral:7b", help="Modele Ollama utilise pour la synthese finale (eval marche + deal score).")
    parser.add_argument("--no-synth", action="store_true", help="Desactive l'etape de synthese finale.")
    parser.add_argument("--synth-timeout", type=float, default=60, help="Timeout Ollama pour l'appel de synthese (modele plus gros = plus lent).")
    parser.add_argument("--http-timeout", type=float, default=10, help="Timeout HTTP en secondes.")
    parser.add_argument("--ollama-timeout", type=float, default=25, help="Timeout Ollama en secondes.")
    parser.add_argument("--max-results", type=int, default=6, help="Nombre de resultats web par requete.")
    parser.add_argument("--fetch-pages", action="store_true", help="Ouvre les pages trouvees pour chercher des prix.")
    parser.add_argument("--verbose", action="store_true", help="Affiche les recherches et sites fouilles en temps reel.")
    parser.add_argument("--delay-min", type=float, default=0.3, help="Latence minimum entre deux requetes HTTP sur un meme domaine non-sensible.")
    parser.add_argument("--delay-max", type=float, default=0.8, help="Latence maximum entre deux requetes HTTP sur un meme domaine non-sensible.")
    parser.add_argument("--retries", type=int, default=2, help="Nombre de retries doux sur HTTP 403/429.")
    parser.add_argument("--top-sources", type=int, default=8, help="Nombre de resultats retenus par Ollama apres tri.")
    parser.add_argument("--no-cache", action="store_true", help="Ignore le cache disque des requetes HTTP.")
    parser.add_argument("--raw", action="store_true", help="Sortie verbeuse (forme {payload, meta}) au lieu de la forme aplatie Claude-compatible.")
    parser.add_argument("--output", help="Fichier de sortie JSON. Par defaut, affiche dans le terminal.")
    return parser.parse_args()


def load_annonce(path, key):
    annonces = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if key not in annonces:
        available = ", ".join(annonces.keys())
        raise SystemExit(f"Annonce introuvable: {key}. Disponibles: {available}")
    return annonces[key]


def _enrich_ad_with_args(ad, args):
    return enrich_ad(
        ad,
        domain_hint=args.domain,
        extract_model=args.model,
        synth_model=args.synth_model,
        fetch_pages=args.fetch_pages,
        fetch_lbc=not args.no_lbc_comparables,
        top_sources=args.top_sources,
        max_results=args.max_results,
        http_timeout=args.http_timeout,
        ollama_timeout=args.ollama_timeout,
        synth_timeout=args.synth_timeout,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        retries=args.retries,
        use_cache=not args.no_cache,
        verbose=args.verbose,
    )


def flatten_result(result):
    """Aplatit {payload, meta} en un seul dict :
    - top-level : ad_id, ad_url, ad_subject, asking_price_eur (tracabilite)
    - puis les cles du payload Claude-compatible (brand, model, ...)
    - puis _sources (signaux web + comparables LBC + durations) pour debug
    """
    payload = result.get("payload") or {}
    meta = result.get("meta") or {}
    flat = {
        "ad_id": meta.get("ad_id"),
        "ad_url": meta.get("ad_url"),
        "ad_subject": meta.get("ad_subject"),
        "asking_price_eur": meta.get("asking_price_eur"),
    }
    flat.update(payload)
    flat["_sources"] = {
        "extracted_identity": meta.get("identity"),
        "msrp_eur_web": (meta.get("web_summary") or {}).get("msrp_eur"),
        "used_eur_web": (meta.get("web_summary") or {}).get("used_eur_web"),
        "web_candidates_count": (meta.get("web_summary") or {}).get("candidates_count"),
        "web_selected_count": (meta.get("web_summary") or {}).get("selected_count"),
        "lbc_comparables_count": (meta.get("lbc_comparables") or {}).get("count"),
        "lbc_comparables_median_eur": (meta.get("lbc_comparables") or {}).get("median_eur"),
        "lbc_comparables_samples": (meta.get("lbc_comparables") or {}).get("samples"),
        "durations_s": meta.get("durations"),
        "models": meta.get("models"),
        "synth_error": meta.get("synth_error"),
    }
    return flat


def _output(result_or_list, output_path, raw=False):
    if raw:
        rendered = json.dumps(result_or_list, ensure_ascii=False, indent=2)
    elif isinstance(result_or_list, list):
        flat = [flatten_result(r) for r in result_or_list]
        rendered = json.dumps(flat, ensure_ascii=False, indent=2)
    else:
        flat = flatten_result(result_or_list)
        rendered = json.dumps(flat, ensure_ascii=False, indent=2)
    if output_path:
        Path(output_path).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


def main():
    global CACHE_ENABLED
    args = parse_args()
    CACHE_ENABLED = not args.no_cache

    if args.no_synth:
        print("[warn] --no-synth ignore en mode pipeline complet (la synthese est integree).", file=sys.stderr)

    if args.annonce:
        annonce_text = load_annonce(args.annonces, args.annonce)
        ad = {"id": None, "subject": args.annonce, "body": annonce_text}
        result = _enrich_ad_with_args(ad, args)
        _output(result, args.output, raw=args.raw)
        return

    if args.ad_json:
        ad = json.loads(Path(args.ad_json).read_text(encoding="utf-8"))
        result = _enrich_ad_with_args(ad, args)
        _output(result, args.output, raw=args.raw)
        return

    if args.lbc_search:
        ads = fetch_lbc_ad(args.lbc_search, limit=args.lbc_limit, verbose=args.verbose)
        if not ads:
            print("[lbc] Aucune annonce trouvee.", file=sys.stderr)
            sys.exit(1)
        results = []
        for idx, ad in enumerate(ads, 1):
            print(f"\n=== [{idx}/{len(ads)}] {ad.get('subject', '')[:80]} ===", file=sys.stderr)
            try:
                results.append(_enrich_ad_with_args(ad, args))
            except Exception as exc:
                print(f"[error] ad {ad.get('id')}: {exc}", file=sys.stderr)
                results.append({"payload": None, "meta": {"ad_id": ad.get("id"), "error": str(exc)}})
        _output(results, args.output)
        return


if __name__ == "__main__":
    main()
