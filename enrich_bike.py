"""Thin CLI wrapper for the bike_agent package.

The real code lives in `bike_agent/`. This module exists to keep the legacy
CLI invocation `python enrich_bike.py ...` working and to provide
backwards-compatible imports `from enrich_bike import enrich_ad`.
"""

from bike_agent import (
    bike_description,
    compact_identity,
    enrich_ad,
    enrich_identity,
    extract_asking_price,
    extract_bike,
    fetch_lbc_ad,
    fetch_lbc_comparables,
    flatten_result,
    get_manufacturer_domain,
    is_junior_bike,
    main,
    render_lbc_ad,
    source_profile_for_url,
    summarize_prices,
    synthesize_evaluation,
    wheel_size_inches,
)

__all__ = [
    "bike_description",
    "compact_identity",
    "enrich_ad",
    "enrich_identity",
    "extract_asking_price",
    "extract_bike",
    "fetch_lbc_ad",
    "fetch_lbc_comparables",
    "flatten_result",
    "get_manufacturer_domain",
    "is_junior_bike",
    "main",
    "render_lbc_ad",
    "source_profile_for_url",
    "summarize_prices",
    "synthesize_evaluation",
    "wheel_size_inches",
]


if __name__ == "__main__":
    main()
