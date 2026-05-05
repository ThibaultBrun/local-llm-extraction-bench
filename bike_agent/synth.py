"""Synthesis step: takes identity + web/LBC signals, produces Claude-compatible payload."""

import json
import time

import ollama

from bike_agent.pages import extract_prices


DECOTE_RULES_BIKE = """
DECOTE VTT/velo occasion (% du prix neuf catalogue):
- < 4 ans : 50-70%
- 4-7 ans : 25-40%
- 8-12 ans : 12-22%  (obsolescence techno)
- > 12 ans : 5-15%

VELOS JUNIOR (roues 14-24 pouces) : decote moins forte (usage moins agressif)
- < 4 ans : 60-80%
- 4-7 ans : 35-55%
- > 8 ans : 15-30%

PENALITES (cumulables):
- 26" adulte sur VTT XC/AM/enduro/DH : -40% au moins (standard obsolete depuis ~2015,
  signal annee <= 2014, geometrie depassee, plus de pieces neuves dispo).
  EXCEPTION : VTT DIRT 26" = NORMAL, c'est le standard de la discipline. Pas de penalite.
  Une bonne affaire en VTT adulte 26" = prix franchement bas (1/3 ou moins du prix
  d'un equivalent moderne 27.5/29 a etat similaire).
- axe 9mm / non-Boost : -20% (incompatible standards modernes 110/148mm).
- cassette 9V/10V : -10% (12V est le standard 2018+).
- cadre alu raye / impact : -10 a -20%.
- modele VTT < 2018 : ne PAS surestimer la cote, plafond strict.

CLASSIFICATION vtt_category (PRINCIPALEMENT par mm de debattement avant/arriere):
- xc            : debattement 80-120mm  (cross-country, leger, perf montee)
- all_mountain  : 120-150mm  (= ancien "trail", polyvalent)
- enduro        : 150-170mm  (descente engagee, montee possible)
- dh            : 180-200mm  (descente pure, double couronne, incl. freeride)
- dirt          : hardtail rigide jump/pumptrack (pas de suspension arriere). TOUJOURS 26" — c'est le standard du dirt, ce n'est PAS un signal d'obsolescence.
- null          : non-VTT (route, gravel, ville, junior, BMX) ou impossible a trancher

Pour determiner vtt_category :
1. Cherche dans l'annonce et les attributs LBC les mots "debattement", "course", "travel", "mm" — signal #1.
2. Sinon deduis du modele connu (ex. Orbea Rallon = enduro 170mm, Rocky Mountain Element = AM 130mm, Specialized Demo = dh 200mm, Orbea Rise = enduro 150-160mm).
3. Indices secondaires : double couronne (= dh), monocouronne + bash guard (= enduro/dh), 1 plateau (= AM+).

condition_score (0-100):
- 0=HS / 30=tres use / 50=usure visible / 80=bon etat / 95+=quasi neuf

deal_score (0-100):
- 0=tres cher / 30=un peu cher / 50=au marche / 70=sous marche -15 a -30% / 90+=>-30%
- Si prix demande inconnu : deal_score = 50 (neutre)
- REGLE: ne PAS baisser le deal_score pour suspicion d'arnaque, ex-location, reconditionne (les doutes vont dans cons).
- Si asking_price est franchement sous le marche (-30% ou plus) il faut OSER monter a 85-95.

DEUX prix neufs distincts a remplir:

PRINCIPE GENERAL : ta connaissance des prix est OBSOLETE (date de l'entrainement, ne suit pas les baisses 2024-2025).
Les SIGNAUX WEB (samples 'msrp' et 'retail') sont la VERITE TERRAIN. Si un revendeur (Alltricks/Bike-Discount/etc.)
vend a 3900 EUR un velo que tu pensais a 13000 EUR, le marche actuel = 3900, point. Trust the web.

msrp_eur = prix catalogue CONSTRUCTEUR (RRP/MSRP) au lancement du modele:
- Si signaux 'msrp' web (constructeur ou magazine) presents et coherents : utilise leur mediane/max.
- Sinon seulement, fallback connaissance catalogue (avec prudence : tes prix peuvent etre obsoletes).
- null si vraiment inconnu.

retail_eur = prix NEUF en boutique chez gros revendeur en ligne (Alltricks, Bike-Discount, Probikeshop, Bike24, Starbike):
- PRIORITE ABSOLUE aux signaux 'retail' du web s'ils existent (un prix Alltricks 2025 ECRASE ta prior 2023).
- Si pas de signal retail mais un msrp web : retail_eur ≈ msrp * 0.85.
- Si vraiment aucun signal et tu connais bien le modele : estimation prudente.
- retail_source : nom du revendeur du signal retail le plus pertinent (le moins cher representatif), ou null.

Plages typiques MSRP (ordre de grandeur):
- VTT enduro carbone haut de gamme : 5000-9000 EUR
- VTT enduro alu mid-range : 2500-4500 EUR
- VTT XC carbone : 4000-8000 EUR
- VAE enduro/AM (Bosch/Shimano EP) : 6000-10000 EUR
- Velo route carbone perf : 3000-12000 EUR
- Velo junior premium 24/26" : 600-1500 EUR

VARIANT TIERS (utile UNIQUEMENT en l'absence de signal web — sinon le web prime):
- "S-Works" (Specialized) : top de gamme historiquement 11000-15000 EUR au lancement.
- "Pro" / "Pro AXS"       : haut de gamme historiquement 7000-11000 EUR.
- "Expert"                : haut milieu historiquement 5000-7500 EUR.
- "Comp"                  : milieu historiquement 3500-5500 EUR.
- "Alloy" / "Alu"         : entree historiquement 2500-4000 EUR.
- "Frameset"              : CADRE SEUL — ne pas confondre avec velo complet.
- "M-Team", "M-LTD" (Orbea) : top series historiquement 9000-14000 EUR.
ATTENTION : ces fourchettes datent. Si retail_eur web pour le MEME variant est plus bas, le web gagne.

INDICES REVENDEUR / EX-LOCATION / RECONDITIONNE (a flagger en CONS sans baisser deal_score):
- "MINT-Bikes", "Buycycle", "Rebike", "Upway", "MyVeloShop" : revendeurs pro de reconditionne, souvent ex-location.
- Mots cles : "garantie X mois", "reconditionne", "occasion certifiee", "ex-location", "ex-flotte", "trustpilot".
- "disponible a la location" dans titre annonce particulier = velo ex-location intensive (usure forte cachee).
- Localisation publication != lieu reel du velo (ex: pub Bordeaux mais stock Aix) = revendeur multi-sites.

CROSS-CHECK l'identite extraite avec ta connaissance catalogue. L'extracteur copie betement les attributs LBC qui peuvent etre faux (vendeur qui se trompe). Exemples a CORRIGER:
- Orbea Rise H10 / Rise H20 / Rise H30 = VAE enduro/AM, 29" ou mullet 29/27.5, JAMAIS 26". MSRP 6000-8000 EUR. Detecte motorisation Shimano EP801 / Bosch / etc.
- Orbea Rallon = enduro 29", 170mm debattement, MSRP 4000-9000 selon tier (M-LTD top).
- Commencal Clash 24 = 24 pouces junior, jamais adulte.
- Specialized Stumpjumper EVO = mullet 29/27.5 ou full 29.
- Specialized S-Works = carbone full, jamais alu. MSRP historique 11000-15000 mais voir signal web.
- Trek Slash / Fuel EX = enduro/trail 29 pouces.
- Indices VAE : "EP801", "EP8", "Bosch CX", "Brose", "Yamaha PWX", "540 Wh", "630 Wh", "Di2 M8150" => electric=true, MSRP minimum 5000-7000 EUR pour le VAE moderne.
Si tu connais avec certitude une caracteristique du modele, ECRASE l'extracteur — meme si extracteur dit 26 pouces, mets 29 pour un Rise H10.

REGLE msrp_eur :
- Si signal MSRP web pour le BON variant : utilise-le directement (web > prior).
- Si signaux MSRP web concernent d'autres variants seulement : fallback sur tier du velo cible.
- Si aucun signal et modele inconnu : null. Sinon commit une valeur plausible.

REGLE PRIORITAIRE : pour estimated_market_eur, le signal le plus fiable est dans cet ordre :
1. mediane LBC TIER-MATCH (>=3 ads exactement du meme tier H10/H30/S-Works/Comp/etc.) — TRES fiable.
2. msrp_eur * decote selon annee (cf table decote) — fiable, calculable.
3. retail_eur web * decote — fiable si retail trouve.
4. mediane LBC GLOBALE (tier mixe) — utilisable mais NON FIABLE si peu de tier-match : un Rise H10 vaut significativement plus qu'un Rise H30 meme si tous les deux apparaissent dans les comparables. Si tier-match < 3 ads, ne PAS utiliser le median global comme reference.

Cas typique d'erreur : asking 2300 EUR pour H10 (top tier ~7000 MSRP), comparables LBC = majoritairement H30 (entree tier ~5000 MSRP) a 2499. Le median 2499 sous-estime la valeur reelle du H10. La bonne base = 7000 * decote_4ans (~0.5) = ~3500 EUR de marche reel. Donc 2300 = -35% sous marche = bon deal (deal_score 75-85).

PRIORITE pour estimated_market_eur (prix REVENTE occasion):
1. Si comparables LBC fournis (ads similaires actuelles) : la mediane est le signal le plus fiable.
2. Sinon : retail_eur * decote selon annee + penalites (ou msrp_eur * decote si retail inconnu).
3. Sinon : fourchette prudente avec reasoning explicite sur l'incertitude.

frame_material : "carbon", "aluminium", "acier", "titane" ou null.
wheel_size : "12", "14", "16", "18", "20", "24", "26", "27.5", "28", "29" ou null.
electric : true si VAE/ebike, false sinon, null si inconnu.
size_label : taille cadre XS/S/M/L/XL/XXL ou null.
"""


SYNTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "brand": {"type": ["string", "null"]},
        "model": {"type": ["string", "null"]},
        "year": {"type": ["integer", "null"]},
        "frame_material": {"type": ["string", "null"]},
        "wheel_size": {"type": ["string", "null"]},
        "electric": {"type": ["boolean", "null"]},
        "size_label": {"type": ["string", "null"]},
        "vtt_category": {
            "type": ["string", "null"],
            "enum": ["xc", "all_mountain", "enduro", "dh", "dirt", None],
        },
        "msrp_eur": {"type": ["number", "null"], "minimum": 0},
        "retail_eur": {"type": ["number", "null"], "minimum": 0},
        "retail_source": {"type": ["string", "null"]},
        "condition_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "estimated_market_eur": {"type": "number", "minimum": 0},
        "deal_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "reasoning": {"type": "string", "maxLength": 1500},
        "pros": {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 4},
        "cons": {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 4},
    },
    "required": [
        "brand", "model", "year", "electric",
        "frame_material", "wheel_size", "size_label", "vtt_category",
        "msrp_eur", "retail_eur", "retail_source",
        "condition_score", "estimated_market_eur", "deal_score",
        "reasoning", "pros", "cons",
    ],
    "additionalProperties": False,
}


def extract_asking_price(annonce):
    text = annonce if isinstance(annonce, str) else (
        f"{annonce.get('subject', '')} {annonce.get('body', '')}"
    )
    prices = extract_prices(text)
    candidates = [p["amount_eur"] for p in prices if 50 <= p["amount_eur"] <= 30000]
    if not candidates:
        return None
    return max(candidates)


def build_synthesis_prompt(annonce, identity, price_summary, asking_price, lbc_comparables=None, domain_hint=None):
    text = annonce if isinstance(annonce, str) else (
        f"{annonce.get('subject', '')}\n\n{annonce.get('body', '')}"
    )
    if len(text) > 2500:
        text = text[:2500] + "..."

    estimate = (price_summary or {}).get("estimate") or {}
    msrp = estimate.get("msrp_eur")
    retail_web = estimate.get("retail_eur")
    used_market_web = estimate.get("used_eur")

    by_kind = (price_summary or {}).get("by_kind") or {}
    web_samples = []
    for kind in ("msrp", "retail", "current", "used", "sale"):
        for p in (by_kind.get(kind) or [])[:3]:
            web_samples.append(f"  - {kind}: {p['amount_eur']} EUR ({p.get('source_name', '?')})")
    web_samples_block = "\n".join(web_samples) or "  (aucun)"

    lbc_comparables = lbc_comparables or []
    lbc_prices_all = [c["price_eur"] for c in lbc_comparables if c.get("price_eur")]
    lbc_prices_tier = [c["price_eur"] for c in lbc_comparables if c.get("price_eur") and c.get("tier_match") is True]

    def _med(values):
        if not values:
            return None
        s = sorted(values)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    lbc_median = _med(lbc_prices_all)
    lbc_median_tier = _med(lbc_prices_tier)
    lbc_block = "  (aucun comparable LBC)"
    if lbc_prices_all:
        n = len(lbc_prices_all)
        n_tier = len(lbc_prices_tier)
        lines = [
            f"  - mediane GLOBALE: {int(lbc_median)} EUR sur {n} ads similaires (tous tiers confondus)",
        ]
        if lbc_median_tier is not None:
            lines.append(f"  - mediane TIER-MATCH (meme version exacte): {int(lbc_median_tier)} EUR sur {n_tier} ads")
            if n_tier < 3:
                lines.append("  ATTENTION: peu de comparables exactement du meme tier. Le median global peut etre biaise par d'autres versions du modele (ex H30 vs H10).")
                lines.append("  -> base-toi PLUTOT sur msrp_eur * decote selon annee.")
        else:
            lines.append("  ATTENTION: aucun comparable du meme tier exact. Median global non fiable pour ce variant.")
            lines.append("  -> base-toi sur msrp_eur * decote selon annee.")
        lines.append(f"  - min/max global: {int(min(lbc_prices_all))} / {int(max(lbc_prices_all))} EUR")
        lines.append("  - echantillons (T = tier-match):")
        for c in lbc_comparables[:6]:
            tag = "T" if c.get("tier_match") is True else "-"
            lines.append(f"    * [{tag}] {c.get('price_eur', '?')} EUR — {(c.get('subject') or '')[:80]}")
        lbc_block = "\n".join(lines)

    domain_line = f"\nDomaine indique par l'amont : {domain_hint}\n" if domain_hint else ""

    return f"""Annonce :
{text}

Identite extraite (extracteur Ollama, peut etre incomplete) :
{json.dumps(identity, ensure_ascii=False, indent=2)}
{domain_line}
Prix demande dans l'annonce : {f'{asking_price} EUR' if asking_price else 'inconnu'}

Comparables LBC actuels (ads similaires, le PLUS FIABLE pour le marche occasion) :
{lbc_block}

Resultats catalogue (recherche web) :
- MSRP constructeur (mediane signaux 'msrp') : {msrp if msrp else 'inconnu'} EUR
- Prix neuf revendeur (mediane signaux 'retail'+'current') : {retail_web if retail_web else 'inconnu'} EUR
- Prix occasion web (mediane 'used'+'sale') : {used_market_web if used_market_web else 'inconnu'} EUR
- Echantillons :
{web_samples_block}

Tache : remplis TOUS les champs du schema.
1. brand/model/year : copie/corrige depuis l'identite. Si l'extracteur s'est trompe et que tu vois mieux dans l'annonce ou via ta connaissance catalogue, ECRASE.
2. frame_material, wheel_size, electric, size_label : extrait depuis l'annonce/identite. CROSS-CHECK avec ta connaissance du modele (ex. Orbea Rise H10 = 29 pouces toujours, donc corrige meme si extracteur dit 27.5). null si vraiment inconnu.
3. vtt_category : enum (xc/all_mountain/enduro/dh/dirt) seulement si VTT, sinon null.
4. msrp_eur : prix CATALOGUE constructeur (RRP). Cf MSRP web + plages typiques + connaissance modele.
5. retail_eur : prix NEUF en boutique chez gros revendeur (Alltricks/Bike-Discount/Probikeshop/Starbike/etc.). Souvent ~85-90% du MSRP. null si modele plus distribue.
   retail_source : nom du revendeur source (string, ex: "Starbike", "Alltricks") quand retail_eur est rempli, sinon null.
6. estimated_market_eur : prix REVENTE occasion. Mediane LBC en priorite, sinon retail_eur * decote selon annee.
7. condition_score (0-100) : depuis le texte ("tres bon etat" ~80, "neuf" 95+, etc.).
8. deal_score (0-100) : ecart prix_demande vs estimated_market_eur. 50 si prix inconnu.
9. reasoning : 2-3 phrases (~200 mots max) expliquant le score + situant le modele.
10. pros : 2-4 bullets concis (max ~10 mots/item).
11. cons : 2-4 bullets concis (max ~10 mots/item).

Sois rigoureux. Si l'identite est lacunaire, dis-le dans reasoning et donne une fourchette prudente.
"""


def synthesize_evaluation(
    model,
    annonce,
    identity,
    price_summary,
    asking_price=None,
    lbc_comparables=None,
    domain_hint=None,
    timeout=60,
    verbose=False,
):
    if verbose:
        print(f"[synth] Ollama model={model}")
    started = time.time()
    client = ollama.Client(timeout=timeout)
    response = client.chat(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "Tu es un expert du marche francais d'occasion (velos, VTT, ebikes, junior, route, gravel, BMX). "
                    "Tu evalues une annonce a partir du texte, de l'identite extraite, "
                    "des prix catalogue trouves sur le web, et des annonces LBC similaires. "
                    "Tu reponds UNIQUEMENT en JSON strict respectant le schema fourni.\n"
                    + DECOTE_RULES_BIKE
                ),
            },
            {
                "role": "user",
                "content": build_synthesis_prompt(
                    annonce, identity, price_summary, asking_price,
                    lbc_comparables=lbc_comparables, domain_hint=domain_hint,
                ),
            },
        ],
        format=SYNTHESIS_SCHEMA,
        options={"temperature": 0},
    )
    data = json.loads(response["message"]["content"])
    duration = time.time() - started
    if verbose:
        retail = data.get('retail_eur')
        retail_src = data.get('retail_source')
        retail_str = f"{retail}EUR ({retail_src})" if retail and retail_src else (f"{retail}EUR" if retail else "?")
        print(
            f"[synth] brand={data.get('brand')} model={data.get('model')} "
            f"vtt_cat={data.get('vtt_category')} wheel={data.get('wheel_size')} "
            f"msrp={data.get('msrp_eur')}EUR retail={retail_str} "
            f"cond={data.get('condition_score')} market={data.get('estimated_market_eur')}EUR "
            f"deal={data.get('deal_score')} ({duration:.2f}s)"
        )
    return data, duration
