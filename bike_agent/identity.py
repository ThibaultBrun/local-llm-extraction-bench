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


ELECTRIC_KEYWORDS = (
    # Designations explicites
    "vae", "vttae", "vtteae", "v.a.e.", "vtt electrique", "velo electrique",
    "vtt e-bike", "ebike", "e-bike", "e-mtb", "emtb", "electric bike",
    "vtt assistance electrique", "assistance electrique", "moteur electrique",
    # Moteurs courants
    "bosch cx", "bosch sx", "performance line", "active line",
    "shimano ep8", "shimano ep801", "ep801", " ep8 ", "shimano steps",
    "brose s mag", "brose drive",
    "yamaha pwx", "yamaha pw",
    "specialized sl", "fazua", "tq hpr50", "polini", "panasonic",
    # Modeles 100% VAE notoires (filet de securite)
    "orbea rise", "orbea wild", "orbea kemen",
    "specialized levo", "specialized kenevo", "specialized vado", "specialized turbo",
    "trek rail", "trek powerfly", "trek fuel exe",
    "scott patron", "scott genius eride", "scott strike eride",
    "cube stereo hybrid", "cube reaction hybrid",
    "haibike sduro", "haibike xduro", "haibike alltrack",
    "moustache samedi", "moustache lundi", "moustache j",
    "decathlon stilus", "rockrider e-",
    "canyon spectral on", "canyon strive on",
    "lapierre overvolt",
    # Batterie
    " wh", "watts heure", "watts-heure", "battery", "batterie",
    "540 wh", "625 wh", "630 wh", "720 wh", "750 wh", "800 wh",
)


NON_ELECTRIC_HINTS = (
    " musculaire", "vtt musculaire", "non electrique", "sans assistance",
)


def detect_electric(text, attributes=None):
    """Detect if the bike is electric (VAE/ebike). Returns True / False / None.

    Strategy:
    1. LBC structured attribute `bicycle_electric` if present (most reliable).
    2. Explicit "musculaire" / "non electrique" keywords -> False.
    3. VAE keywords (VAE, motor name, battery Wh, known ebike model) -> True.
    4. None if no signal.
    """
    attrs = attributes or {}
    raw_attr = str(attrs.get("bicycle_electric") or attrs.get("electric") or "").lower().strip()
    if raw_attr in {"true", "yes", "oui", "1"}:
        return True
    if raw_attr in {"false", "no", "non", "0"}:
        return False

    if not text:
        return None
    lower = " " + text.lower() + " "

    for hint in NON_ELECTRIC_HINTS:
        if hint in lower:
            return False

    for kw in ELECTRIC_KEYWORDS:
        if kw in lower:
            return True
    return None


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
    annonce_attrs = annonce.get("attributes") if isinstance(annonce, dict) else None
    tier = detect_variant_tier(annonce_text)
    if tier and not identity.get("version"):
        identity["version"] = tier

    electric = detect_electric(annonce_text, annonce_attrs)
    if electric is not None:
        identity["electric"] = electric

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
    """Build a query suffix from wheel size — only useful for junior bikes
    (14-24 pouces) where the wheel size is fundamental to disambiguate from
    the adult variant. For adult sizes (26/27.5/29) the model name uniquely
    identifies the bike and the wheel suffix only adds noise (and amplifies
    extractor errors when LBC attributes are wrong)."""
    size = wheel_size_inches(identity)
    if size is None:
        return ""
    if 14 <= size <= 24:
        label = int(size) if size.is_integer() else size
        return f' "{label} pouces" junior enfant'
    return ""


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
