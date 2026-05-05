"""Orchestration: enrich_identity (web search + ranking + price extraction) and enrich_ad (full pipeline)."""

import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

from bike_agent import config
from bike_agent.identity import (
    extract_bike,
    get_manufacturer_domain,
    source_profile_for_url,
)
from bike_agent.lbc import fetch_lbc_comparables, render_lbc_ad
from bike_agent.pages import (
    extract_prices,
    extract_prices_with_llm,
    fetch_page_text,
    format_prices,
)
from bike_agent.ranking import build_search_queries, rank_sources_with_llm
from bike_agent.search import web_search
from bike_agent.synth import extract_asking_price, synthesize_evaluation


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
            "by_kind": {"msrp": [], "retail": [], "current": [], "used": [], "sale": [], "unknown": []},
            "estimate": {"msrp_eur": None, "retail_eur": None, "used_eur": None},
        }

    unique = []
    seen = set()
    for price in sorted(prices, key=lambda item: (item["source_priority"], item["amount_eur"])):
        key = (price["amount_eur"], price["kind"], price["source"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(price)

    by_kind = {"msrp": [], "retail": [], "current": [], "used": [], "sale": [], "unknown": []}
    for price in unique:
        kind = price["kind"] if price["kind"] in by_kind else "unknown"
        by_kind[kind].append(price)

    msrp_pool = [p["amount_eur"] for p in by_kind["msrp"]]
    retail_pool = [p["amount_eur"] for p in by_kind["retail"] + by_kind["current"]]
    used_pool = [p["amount_eur"] for p in by_kind["used"] + by_kind["sale"]]

    return {
        "count": len(unique),
        "by_kind": {
            "msrp": by_kind["msrp"][:10],
            "retail": by_kind["retail"][:10],
            "current": by_kind["current"][:10],
            "used": by_kind["used"][:10],
            "sale": by_kind["sale"][:5],
            "unknown": by_kind["unknown"][:10],
        },
        "estimate": {
            "msrp_eur": _median(msrp_pool),
            "retail_eur": _median(retail_pool),
            "used_eur": _median(used_pool),
        },
    }


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
                query, max_results=max_results, timeout=http_timeout,
                delay_min=delay_min, delay_max=delay_max,
                retries=retries, verbose=verbose,
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
            if any(netloc.endswith(d) for d in config.EXCLUDED_RESULT_DOMAINS):
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
                    "source_type": source_profile.get("type", "other"),
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
                timeout=http_timeout, delay_min=delay_min, delay_max=delay_max,
                retries=retries, verbose=verbose,
            )
            enriched["page_fetch_ok"] = page["ok"]
            enriched["page_fetch_error"] = page["error"]
            enriched["page_fetch_via"] = page.get("via")

            page_prices = []
            if page["ok"]:
                try:
                    page_prices = extract_prices_with_llm(
                        model, identity, page["text"], result["url"],
                        source_profile=result,
                        timeout=ollama_timeout, verbose=verbose,
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
            "user_agent_rotation": len(config.USER_AGENTS),
        },
        "price_sources": {
            "manufacturer_domain": get_manufacturer_domain(identity),
            "catalogue_sources": config.PRICE_SOURCE_PROFILES,
            "future_geometry_sources": config.FUTURE_GEOMETRY_SOURCES,
        },
        "search_runs": search_runs,
        "candidates_count": len(candidates),
        "rank_method": rank_method,
        "selected_results": enriched_results,
        "price_summary": summarize_prices(enriched_results),
    }


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
    saved_cache = config.CACHE_ENABLED
    config.CACHE_ENABLED = use_cache

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
            identity=identity, model=extract_model, max_results=max_results,
            fetch_pages=fetch_pages, http_timeout=http_timeout,
            ollama_timeout=ollama_timeout, delay_min=delay_min, delay_max=delay_max,
            retries=retries, top_sources=top_sources, verbose=verbose,
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
            model=synth_model, annonce=annonce_text, identity=identity,
            price_summary=web.get("price_summary"), asking_price=asking_price,
            lbc_comparables=comparables, domain_hint=domain_hint,
            timeout=synth_timeout, verbose=verbose,
        )
    except Exception as exc:
        if verbose:
            print(f"[synth:error] {exc}")
        synth_error = str(exc)

    config.CACHE_ENABLED = saved_cache

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
            "msrp_eur": (web.get("price_summary") or {}).get("estimate", {}).get("msrp_eur"),
            "retail_eur": (web.get("price_summary") or {}).get("estimate", {}).get("retail_eur"),
            "retail_source": None,
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
                "retail_eur_web": (web.get("price_summary") or {}).get("estimate", {}).get("retail_eur"),
                "used_eur_web": (web.get("price_summary") or {}).get("estimate", {}).get("used_eur"),
                "retail_samples": [
                    {
                        "amount_eur": p["amount_eur"],
                        "source_name": p.get("source_name"),
                        "source_domain": p.get("source_domain"),
                    }
                    for p in (web.get("price_summary") or {}).get("by_kind", {}).get("retail", [])[:5]
                ],
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
