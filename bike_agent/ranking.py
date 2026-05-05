"""Search query generation + LLM-based ranking of search results."""

import json

import ollama

from bike_agent.identity import (
    bike_description,
    compact_identity,
    get_manufacturer_domain,
    is_junior_bike,
    search_query_suffix,
)


def build_search_queries(identity):
    base = compact_identity(identity)
    if not base:
        return []

    suffix = search_query_suffix(identity)
    queries = [
        {"source": "Revendeurs", "domain": None, "query": f"{base}{suffix} prix neuf alltricks bike-discount probikeshop"},
        {"source": "Web general", "domain": None, "query": f"{base}{suffix} prix fiche technique"},
        {"source": "Web general", "domain": None, "query": f"{base}{suffix} test review velo"},
    ]

    manufacturer_domain = get_manufacturer_domain(identity)
    if manufacturer_domain:
        queries.insert(
            0,
            {
                "source": "Constructeur",
                "domain": manufacturer_domain,
                "query": f"{base}{suffix} site:{manufacturer_domain}",
            },
        )
    return queries


RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "selected": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["i", "reason"],
            },
        }
    },
    "required": ["selected"],
}


def rank_sources_with_llm(model, identity, candidates, top_k=8, timeout=25, verbose=False):
    if not candidates:
        return []

    bike_desc = bike_description(identity)
    junior_warning = (
        "ATTENTION: il s'agit d'un velo JUNIOR/ENFANT. Rejette toute page qui parle "
        "de la version adulte du meme modele (roues 26\", 27.5\", 29\"). Garde uniquement "
        "les pages qui mentionnent explicitement la taille de roues correspondante.\n"
        if is_junior_bike(identity)
        else ""
    )
    payload = [
        {
            "i": index,
            "title": (candidate.get("title") or "")[:200],
            "url": candidate["url"],
            "snippet": (candidate.get("snippet") or "")[:300],
            "source": candidate.get("source_name"),
        }
        for index, candidate in enumerate(candidates)
    ]

    prompt = (
        f"Velo cible:\n{bike_desc}\n\n"
        f"{junior_warning}"
        f"Voici une liste de resultats web (titre, url, extrait, source). "
        f"Selectionne UNIQUEMENT ceux qui correspondent EXACTEMENT a ce velo "
        f"(meme marque, meme modele, ET meme taille de roues — un velo 24 pouces "
        f"n'est PAS le meme produit qu'un 27.5 ou un 29 pouces). "
        f"Ils doivent contenir le PRIX NEUF, la FICHE TECHNIQUE ou la GEOMETRIE. "
        f"Ignore: autres tailles de roues, autres modeles, vetements/accessoires, "
        f"forums sans info technique, resultats hors sujet. "
        f"Limite a {top_k} resultats max, par ordre de pertinence decroissante. "
        f"Pour chaque resultat retenu, fournis i (l'index entier) et une raison courte.\n\n"
        f"Resultats:\n{json.dumps(payload, ensure_ascii=False)}"
    )

    if verbose:
        print(f"[rank] Ollama model={model}, candidats={len(candidates)}")

    client = ollama.Client(timeout=timeout)
    response = client.chat(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "Tu tries des resultats web pour identifier les sources techniques et de prix d'un velo. Tu reponds uniquement en JSON strict.",
            },
            {"role": "user", "content": prompt},
        ],
        format=RANK_SCHEMA,
        options={"temperature": 0},
    )

    data = json.loads(response["message"]["content"])
    selected = []
    seen_idx = set()
    for item in data.get("selected", []):
        idx = item.get("i")
        if not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
            continue
        if idx in seen_idx:
            continue
        seen_idx.add(idx)
        selected.append({**candidates[idx], "llm_reason": item.get("reason", "")})
        if len(selected) >= top_k:
            break

    if verbose:
        for entry in selected:
            print(f"[rank:keep] {entry['url']} — {(entry.get('llm_reason') or '')[:80]}")

    return selected
