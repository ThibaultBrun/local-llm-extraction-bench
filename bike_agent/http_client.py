"""HTTP layer: safe_url, headers (incl. Jina auth), throttle per domain, cache, http_get."""

import hashlib
import random
import re
import time
from html import unescape
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from bike_agent import config


def normalize_space(value):
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def random_user_agent():
    return random.choice(config.USER_AGENTS)


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

    if config.JINA_API_KEY:
        host = urlparse(url).netloc.lower()
        if host.endswith("r.jina.ai") or host.endswith("s.jina.ai"):
            headers["Authorization"] = f"Bearer {config.JINA_API_KEY}"

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
    return config.CACHE_DIR / digest[:2] / f"{digest}.html"


def cache_read(url, max_age=None):
    if max_age is None:
        max_age = config.CACHE_TTL_SECONDS
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
    for known, interval in config.DOMAIN_MIN_INTERVAL.items():
        if domain.endswith(known):
            min_interval = interval
            break

    elapsed = time.time() - config.LAST_REQUEST_TIME.get(domain, 0)
    if elapsed >= min_interval:
        wait = random.uniform(0.0, max(default_max - default_min, 0.1))
    else:
        wait = (min_interval - elapsed) + random.uniform(0.1, 0.4)

    if wait > 0:
        if verbose:
            print(f"[throttle] {domain} sleep {wait:.2f}s (interval {min_interval}s, since last {elapsed:.1f}s)")
        time.sleep(wait)
    config.LAST_REQUEST_TIME[domain] = time.time()


def safe_url(url):
    return quote(url, safe=config.SAFE_URL_CHARS)


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
        use_cache = config.CACHE_ENABLED
    if use_cache:
        cached = cache_read(url)
        if cached is not None:
            if verbose:
                print(f"[cache:hit] {url}")
            return cached

    last_error = None
    for attempt in range(retries + 1):
        throttle_for_domain(
            url, default_min=delay_min, default_max=delay_max, verbose=verbose,
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
