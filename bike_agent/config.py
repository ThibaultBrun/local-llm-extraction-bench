"""Constants and runtime config (env loading, cache state, throttle table)."""

import os
import re
from pathlib import Path


def load_env_file(path=".env"):
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


# Loaded eagerly so that constants below see the env vars.
load_env_file()
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
    r"(?<!\d)(\d{1,3}(?:[ .]\d{3})*|\d{3,6})(?:[,.]\d{1,2})?\s*(?:€|eur|euros)(?![a-z])",
    flags=re.IGNORECASE,
)

PRICE_CONTEXT_RE = re.compile(r"(?i)(?:€|\beur\b|\beuros?\b)")

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
    "kona": "konaworld.com",
    "ktm": "ktm-bikes.at",
    "lapierre": "lapierrebikes.com",
    "marin": "marinbikes.com",
    "megamo": "megamo.com",
    "mondraker": "mondraker.com",
    "norco": "norco.com",
    "orbea": "orbea.com",
    "pivot": "pivotcycles.com",
    "propain": "propain-bikes.com",
    "radon": "radon-bikes.de",
    "rockrider": "decathlon.fr",
    "rocky mountain": "bikes.com",
    "santa cruz": "santacruzbicycles.com",
    "scott": "scott-sports.com",
    "specialized": "specialized.com",
    "sunn": "sunn.fr",
    "trek": "trekbikes.com",
    "transition": "transitionbikes.com",
    "vitus": "vitusbikes.com",
    "yeti": "yeticycles.com",
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

KNOWN_RETAILERS = [
    {"name": "Alltricks", "domain": "alltricks.fr"},
    {"name": "Probikeshop", "domain": "probikeshop.fr"},
    {"name": "Bike-Discount", "domain": "bike-discount.de"},
    {"name": "Bike24", "domain": "bike24.com"},
    {"name": "Bike-Components", "domain": "bike-components.de"},
    {"name": "Starbike", "domain": "starbike.com"},
    {"name": "Mantel", "domain": "mantel.com"},
    {"name": "Bikester", "domain": "bikester.fr"},
    {"name": "Cyclable", "domain": "cyclable.com"},
    {"name": "Materiel-Velo", "domain": "materiel-velo.com"},
    {"name": "Lecyclo", "domain": "lecyclo.com"},
    {"name": "Wiggle", "domain": "wiggle.com"},
    {"name": "Chain Reaction Cycles", "domain": "chainreactioncycles.com"},
    {"name": "Tredz", "domain": "tredz.co.uk"},
    {"name": "Cycles UK", "domain": "cyclesuk.com"},
    {"name": "Hibike", "domain": "hibike.com"},
    {"name": "Rose Bikes", "domain": "rosebikes.fr"},
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

LBC_ATTR_SKIP_KEYS = {
    "profile_picture_url", "rating_score", "rating_count", "is_bundleable",
    "purchase_cta_visible", "negotiation_cta_visible", "country_isocode3166",
    "shipping_type", "shippable", "is_import", "vehicle_available_payment_methods",
    "vehicle_is_eligible_p2p", "estimated_parcel_size", "estimated_parcel_weight",
    "payment_methods", "stock_quantity", "activity_sector", "argus_object_id",
    "spare_parts_availability", "bicycode",
}
