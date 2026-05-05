"""Web search backends: DuckDuckGo (lite + via Jina), Bing, Jina native search."""

import base64
import re
from html import unescape
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from bike_agent import config
from bike_agent.http_client import http_get, normalize_space


def unwrap_redirect_url(url):
    """Resolve common search-redirect URLs to their actual destination.

    Bing returns results via `https://www.bing.com/ck/a?...&u=a1<base64>&...`
    where the `u` parameter (after stripping the `a1` prefix) is the base64-
    encoded destination URL. Until we unwrap, all such results look like
    `bing.com` to the source classifier (and price extraction reads the
    wrong page entirely).
    """
    if not url:
        return url
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if parsed.netloc.endswith("bing.com") and "/ck/a" in parsed.path:
        target = parse_qs(parsed.query).get("u", [None])[0]
        if target and target.startswith("a1"):
            try:
                padding = "=" * (-len(target[2:]) % 4)
                return base64.urlsafe_b64decode(target[2:] + padding).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return url
    return url


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


def unique_results(results, max_results):
    unique = []
    seen = set()
    for result in results:
        # Resolve Bing/redirect wrappers so source classification sees the real domain
        result = {**result, "url": unwrap_redirect_url(result["url"])}
        if result["url"] in seen:
            continue
        seen.add(result["url"])
        unique.append(result)
        if len(unique) >= max_results:
            break
    return unique


def duckduckgo_search(query, max_results=8, timeout=10, delay_min=0, delay_max=0, retries=0, verbose=False):
    url = f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}"
    if verbose:
        print(f"[visit:search] duckduckgo | {url}")
    html = http_get(
        url, timeout=timeout, delay_min=delay_min, delay_max=delay_max,
        retries=retries, verbose=verbose, referer="https://duckduckgo.com/",
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
        url, timeout=timeout, delay_min=delay_min, delay_max=delay_max,
        retries=retries, verbose=verbose, referer="https://www.bing.com/",
    )
    parser = BingParser()
    parser.feed(html)
    return unique_results(parser.results, max_results)


def jina_duckduckgo_search(query, max_results=8, timeout=10, delay_min=0, delay_max=0, retries=0, verbose=False):
    url = f"https://r.jina.ai/https://duckduckgo.com/html/?q={quote_plus(query)}"
    if verbose:
        print(f"[visit:search] duckduckgo+jina | {url}")
    markdown = http_get(
        url, timeout=timeout, delay_min=delay_min, delay_max=delay_max,
        retries=retries, verbose=verbose, referer="https://r.jina.ai/",
    )
    results = []
    matches = list(re.finditer(r"^## \[([^\]]+)\]\(([^)]+)\)", markdown, flags=re.MULTILINE))
    for index, match in enumerate(matches):
        title = normalize_space(match.group(1))
        target_url = clean_duckduckgo_url(match.group(2))
        if not title or not target_url or is_duckduckgo_internal(target_url):
            continue
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        body = markdown[match.end() : next_start]
        body = re.sub(r"\[[^\]]*\]\([^)]+\)", " ", body)
        snippet = normalize_space(body)
        results.append({"title": title, "url": target_url, "snippet": snippet[:500]})
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
        url, timeout=timeout, delay_min=delay_min, delay_max=delay_max,
        retries=retries, verbose=verbose, referer="https://jina.ai/",
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
    if config.JINA_API_KEY:
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
                query, max_results=max_results, timeout=timeout,
                delay_min=delay_min, delay_max=delay_max,
                retries=retries, verbose=verbose,
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
