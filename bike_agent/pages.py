"""Page text fetching (Jina Reader first, direct fallback) + price extraction (regex + LLM)."""

import json
import re
from urllib.error import HTTPError, URLError

import ollama

from bike_agent import config
from bike_agent.http_client import http_get, normalize_space
from bike_agent.identity import bike_description, is_junior_bike


def parse_price_amount(raw):
    value = re.sub(r"(?i)\s*(€|eur|euros)\s*$", "", raw).strip()
    value = value.replace(" ", "")
    if re.search(r"[,.]\d{1,2}$", value):
        value = re.split(r"[,.]", value)[0]
    value = value.replace(".", "")
    if not value.isdigit():
        return None
    return int(value)


def extract_prices(text):
    prices = []
    seen = set()
    for match in config.PRICE_RE.finditer(text or ""):
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
            jina_url, timeout=timeout, delay_min=delay_min, delay_max=delay_max,
            retries=retries, verbose=verbose,
        )
        return {"ok": True, "error": None, "text": text[:250000], "via": "jina"}
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
        if verbose:
            print(f"[fetch:jina:fail] {exc} -> fallback direct")

    try:
        html = http_get(
            url, timeout=timeout, delay_min=delay_min, delay_max=delay_max,
            retries=retries, verbose=verbose,
        )
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc), "text": "", "via": "direct"}

    text = html_to_text(html)[:250000]
    return {"ok": True, "error": None, "text": text, "via": "direct"}


def extract_price_context(text, window=400, max_chunks=8):
    if not text:
        return ""
    chunks = []
    last_end = -1
    for match in config.PRICE_CONTEXT_RE.finditer(text):
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
                        "enum": ["msrp", "retail", "current", "used", "sale", "unknown"],
                    },
                    "context": {"type": "string"},
                },
                "required": ["amount_eur", "kind"],
            },
        }
    },
    "required": ["prices"],
}


def extract_prices_with_llm(model, identity, page_text, source_url, source_profile=None, timeout=25, verbose=False):
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

    sp = source_profile or {}
    source_type = sp.get("source_type") or sp.get("type") or "other"
    source_name = sp.get("source_name") or sp.get("name") or "?"
    source_hint = ""
    if source_type == "manufacturer":
        source_hint = (
            f"\nSOURCE TYPE: site CONSTRUCTEUR ({source_name}). "
            f"REGLE STRICTE: kind ne peut etre QUE 'msrp' (prix tarif catalogue) ou 'sale' "
            f"(promotion explicite type -X% / soldes). Ne JAMAIS utiliser 'retail' pour une "
            f"page constructeur — un fabricant n'est pas un revendeur, meme s'il vend en direct.\n"
        )
    elif source_type == "retailer":
        source_hint = (
            f"\nSOURCE TYPE: gros revendeur en ligne ({source_name}, type Alltricks/Bike-Discount). "
            f"REGLE STRICTE: le prix de vente actuel = 'retail'. Si un prix est barre = 'msrp', "
            f"l'actuel = 'retail' (ou 'sale' si promo explicite -X%).\n"
        )
    elif source_type == "magazine":
        source_hint = (
            f"\nSOURCE TYPE: magazine/comparateur ({source_name}). "
            f"Le prix mentionne est generalement le MSRP de reference (kind='msrp'). "
            f"Ne pas utiliser 'retail' pour un magazine.\n"
        )

    excerpt = extract_price_context(page_text)

    prompt = (
        f"Velo cible:\n{bike_desc}\n"
        f"Source: {source_url}"
        f"{source_hint}\n"
        f"{junior_warning}"
        f"Voici des extraits d'une page web autour de mentions de prix. "
        f"Identifie UNIQUEMENT les prix qui correspondent au velo cible "
        f"(meme marque, meme modele, MEME taille de roues — pas les composants seuls, "
        f"pas d'autres tailles/modeles, pas les accessoires). "
        f"Pour chaque prix retenu:\n"
        f"- amount_eur: montant entier en euros\n"
        f"- kind:\n"
        f"  * 'msrp' = prix CATALOGUE constructeur (RRP, prix tarif, prix barre sur fiche constructeur)\n"
        f"  * 'retail' = prix NEUF en boutique chez gros revendeur en ligne (Alltricks, Bike-Discount, Probikeshop)\n"
        f"  * 'current' = prix neuf actuel autre source (magazine, comparateur)\n"
        f"  * 'used' = occasion\n"
        f"  * 'sale' = promotion explicite (-X%, soldes)\n"
        f"  * 'unknown' = ambigu\n"
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
        if kind not in {"msrp", "retail", "current", "used", "sale", "unknown"}:
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
