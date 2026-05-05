"""Identity extraction (Ollama) + helpers (manufacturer/retailer detection)."""

import json
import re
import time
from urllib.parse import urlparse

import ollama

from benchmark_extraction import build_prompt, post_process, schema

from bike_agent import config
from bike_agent.http_client import normalize_space


VARIANT_TIERS = (
    "S-Works", "Pro AXS", "Pro Carbon", "Pro", "Expert", "Comp", "Alloy", "Frameset",
    "M-Team", "M-LTD", "M10", "M20", "M30", "M-LR",
    "H10", "H20", "H30",
    "Master", "Team", "LTD", "Race", "Limited",
    "AXS", "GX", "X01", "XX1", "XTR",
)


def detect_variant_tier(text):
    """Scan annonce text for known variant/tier keywords (e.g. S-Works, Pro,
    Expert) that the schema-based extractor often drops. Returns the matched
    label or None."""
    if not text:
        return None
    lower = text.lower()
    for tier in VARIANT_TIERS:
        pattern = re.compile(r"(?<![a-z])" + re.escape(tier.lower()) + r"(?![a-z])")
        if pattern.search(lower):
            return tier
    return None


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

    annonce_text = annonce if isinstance(annonce, str) else (
        f"{annonce.get('subject', '')} {annonce.get('body', '')}"
    )
    tier = detect_variant_tier(annonce_text)
    if tier and not identity.get("version"):
        identity["version"] = tier

    if verbose:
        print(f"[extract] identity={json.dumps(identity, ensure_ascii=False)}")
    return identity, time.time() - start


def compact_identity(identity, include_version=True):
    """Build a search-friendly label.

    Default order: marque + tier(version) + modele + annee, natural for
    product names (e.g. "SPECIALIZED S-Works Stumpjumper EVO 2024").
    Pass include_version=False for the no-tier fallback query.
    """
    version = identity.get("version") if include_version else None
    parts = [
        identity.get("marque"),
        version,
        identity.get("modele"),
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
        if candidate in config.MANUFACTURER_DOMAINS:
            return config.MANUFACTURER_DOMAINS[candidate]
    return None


def source_profile_for_url(url, identity=None):
    parsed = urlparse(url)
    domain = parsed.netloc.lower().removeprefix("www.")

    manufacturer_domain = get_manufacturer_domain(identity or {})
    if manufacturer_domain and domain.endswith(manufacturer_domain):
        return {"name": "Constructeur", "domain": manufacturer_domain, "priority": 10, "type": "manufacturer"}

    for retailer in config.KNOWN_RETAILERS:
        if domain.endswith(retailer["domain"]):
            return {"name": retailer["name"], "domain": retailer["domain"], "priority": 15, "type": "retailer"}

    for profile in config.PRICE_SOURCE_PROFILES:
        if domain.endswith(profile["domain"]):
            return {**profile, "type": "magazine"}

    return {"name": "Autre", "domain": domain, "priority": 999, "type": "other"}
