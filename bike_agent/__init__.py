"""bike-ia-agent - Local-LLM agent for enriching Leboncoin bike ads.

Public API:
    from bike_agent import enrich_ad, fetch_lbc_ad, fetch_lbc_comparables

See README for the pipeline overview and output schema.
"""

from bike_agent.config import load_env_file as _load_env_file

_load_env_file()

from bike_agent.identity import (
    extract_bike,
    compact_identity,
    bike_description,
    is_junior_bike,
    wheel_size_inches,
    get_manufacturer_domain,
    source_profile_for_url,
)
from bike_agent.lbc import fetch_lbc_ad, fetch_lbc_ad_by_id, fetch_lbc_comparables, render_lbc_ad
from bike_agent.synth import synthesize_evaluation, extract_asking_price
from bike_agent.pipeline import enrich_ad, enrich_identity, summarize_prices
from bike_agent.cli import flatten_result, main

__all__ = [
    "enrich_ad",
    "enrich_identity",
    "summarize_prices",
    "fetch_lbc_ad",
    "fetch_lbc_ad_by_id",
    "fetch_lbc_comparables",
    "render_lbc_ad",
    "synthesize_evaluation",
    "extract_asking_price",
    "extract_bike",
    "compact_identity",
    "bike_description",
    "is_junior_bike",
    "wheel_size_inches",
    "get_manufacturer_domain",
    "source_profile_for_url",
    "flatten_result",
    "main",
]
