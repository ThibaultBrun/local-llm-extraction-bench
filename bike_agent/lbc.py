"""Leboncoin integration: render an ad to text, fetch live ads, fetch comparables."""

from bike_agent import config


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
        if k not in config.LBC_ATTR_SKIP_KEYS and v
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
