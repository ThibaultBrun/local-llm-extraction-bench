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
- 26" sortie avant ~2015 : -30% (standard obsolete)
- axe 9mm / non-Boost : -20%
- cassette 9V/10V : -10%
- cadre alu raye / impact : -10 a -20%
- modele VTT < 2018 : ne PAS surestimer la cote

CLASSIFICATION vtt_category (PRINCIPALEMENT par mm de debattement avant/arriere):
- xc            : debattement 80-120mm  (cross-country, leger, perf montee)
- all_mountain  : 120-150mm  (= ancien "trail", polyvalent)
- enduro        : 150-170mm  (descente engagee, montee possible)
- dh            : 180-200mm  (descente pure, double couronne, incl. freeride)
- dirt          : hardtail rigide jump/pumptrack (pas de suspension arriere)
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
- REGLE: ne PAS baisser le deal_score pour suspicion d'arnaque (les doutes vont dans cons).

DEUX prix neufs distincts a remplir:

msrp_eur = prix catalogue CONSTRUCTEUR (RRP/MSRP) au lancement du modele:
- Le prix tarif officiel publie par la marque (Orbea, Trek, Specialized, etc.).
- Utilise les MSRP web s'ils sont plausibles, sinon corrige avec ta connaissance catalogue.
- null si vraiment inconnu.

retail_eur = prix NEUF en boutique chez gros revendeur en ligne (Alltricks, Bike-Discount, Probikeshop, Bike24):
- C'est le prix REELLEMENT pratique aujourd'hui par les revendeurs (souvent decote vs MSRP : -10 a -30%).
- Source primaire = signaux 'retail' du resume web. Sinon estime: msrp * 0.85 (decote moyenne revendeur).
- Si le velo n'est plus distribue chez les revendeurs (modele >2-3 ans), retail_eur peut etre null.

Plages typiques MSRP (ordre de grandeur):
- VTT enduro carbone haut de gamme : 5000-9000 EUR
- VTT enduro alu mid-range : 2500-4500 EUR
- VTT XC carbone : 4000-8000 EUR
- VAE enduro/AM (Bosch/Shimano EP) : 6000-10000 EUR
- Velo route carbone perf : 3000-12000 EUR
- Velo junior premium 24/26" : 600-1500 EUR

CROSS-CHECK l'identite extraite avec ta connaissance catalogue. Exemples:
- Orbea Rise H10 = TOUJOURS 29 pouces (corrige wheel_size si extracteur dit 27.5).
- Commencal Clash 24 = 24 pouces junior (jamais adulte).
- Specialized Stumpjumper EVO = mullet 29/27.5 ou full 29.
Si tu connais avec certitude une caracteristique du modele, ECRASE l'extracteur.

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
        "msrp_eur", "retail_eur", "condition_score", "estimated_market_eur", "deal_score",
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
    lbc_prices = [c["price_eur"] for c in lbc_comparables if c.get("price_eur")]
    lbc_block = "  (aucun comparable LBC)"
    lbc_median = None
    if lbc_prices:
        sorted_prices = sorted(lbc_prices)
        n = len(sorted_prices)
        lbc_median = sorted_prices[n // 2] if n % 2 else (sorted_prices[n // 2 - 1] + sorted_prices[n // 2]) / 2
        lbc_block_lines = [
            f"  - mediane: {int(lbc_median)} EUR sur {n} ads",
            f"  - min/max: {int(min(lbc_prices))} / {int(max(lbc_prices))} EUR",
            "  - echantillons:",
        ]
        for c in lbc_comparables[:6]:
            lbc_block_lines.append(f"    * {c.get('price_eur', '?')} EUR — {(c.get('subject') or '')[:80]}")
        lbc_block = "\n".join(lbc_block_lines)

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
