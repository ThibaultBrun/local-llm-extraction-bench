# bike-ia-agent

Agent local d'enrichissement d'annonces velo (Leboncoin) — extraction d'identite, recherche web, comparables LBC, et synthese de marche — sans dependance a une API LLM payante.

Concu comme drop-in replacement du Claude-CLI utilise par [lbc-sniper](../lbc-sniper), avec un meilleur signal de prix marche grace aux comparables LBC en temps reel.

## Pipeline

1. **Extraction** (Ollama `llama3.2:3b`) — marque, modele, annee, taille de roues, taille cadre depuis le texte de l'annonce + attributs LBC.
2. **Recherche catalogue** (Jina Reader + Ollama) — MSRP via les fiches constructeur (Cloudflare contourne par Jina), prix occasion sur magazines specialises (Velo Vert, Pinkbike, 99 Spokes, etc.).
3. **Comparables LBC** (lib `lbc`) — annonces similaires actuelles pour calibrer le marche reel (signal le plus fiable).
4. **Synthese** (Ollama `mistral:7b`) — `condition_score`, `estimated_market_eur`, `deal_score`, `reasoning`, `pros`, `cons`. Schema Claude-compatible.

## Pre-requis

- Python 3.10+
- [Ollama](https://ollama.com/) avec les modeles :
  ```bash
  ollama pull llama3.2:3b
  ollama pull mistral:7b
  ```
- Cle API Jina (gratuite, signup sur [jina.ai](https://jina.ai/)) — 500 RPM, contourne Cloudflare sur les sites constructeur.

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env       # puis remplir JINA_API_KEY
```

## Usage

### CLI : enrichir une annonce

```powershell
# Search live sur Leboncoin
python .\enrich_bike.py --lbc-search "vtt orbea rallon" --lbc-limit 3 --fetch-pages --output samples\result.json

# Depuis un dict LBC en JSON local
python .\enrich_bike.py --ad-json my-ad.json --domain vtt_enduro --fetch-pages

# Depuis le pack de fixtures (test legacy)
python .\enrich_bike.py --annonce orbea_rise_h10 --fetch-pages --verbose
```

Flags utiles :
- `--fetch-pages` : ouvre les pages trouvees pour extraire les prix via Ollama
- `--verbose` : log toutes les etapes (search, throttle, fetch, rank, synth)
- `--no-lbc-comparables` : skip la recherche d'annonces similaires sur LBC
- `--no-cache` : ignore le cache disque HTTP
- `--raw` : sortie verbeuse `{payload, meta}` au lieu de la forme aplatie

### API Python (consommation par lbc-sniper)

```python
from enrich_bike import enrich_ad

result = enrich_ad(
    ad={
        "id": 123,
        "subject": "VTT Enduro Orbea Rallon M10 2023",
        "body": "...",
        "price": 4500,
        "url": "https://www.leboncoin.fr/...",
        "city": "Bayonne",
        "attributes": {"bicycle_wheel_size": "29\"", ...},
    },
    domain_hint="vtt_enduro",       # depuis classify_vtt() en amont
    extract_model="llama3.2:3b",    # rapide pour identite + tri
    synth_model="mistral:7b",       # plus fort pour l'evaluation
)
# result["payload"] = drop-in pour update_enrichment de lbc-sniper
# result["meta"]    = identite, durations, sources web, comparables LBC
```

### Format de sortie (Claude-compatible)

```json
{
  "ad_id": 123,
  "ad_url": "...",
  "ad_subject": "...",
  "asking_price_eur": 4500,
  "brand": "Orbea",
  "model": "Rallon",
  "year": 2023,
  "frame_material": "carbon",
  "wheel_size": "29",
  "electric": false,
  "size_label": "M",
  "vtt_category": "enduro",
  "condition_score": 85,
  "estimated_market_eur": 4200,
  "deal_score": 60,
  "reasoning": "...",
  "pros": ["..."],
  "cons": ["..."],
  "_sources": {
    "extracted_identity": {...},
    "msrp_eur_web": 5999,
    "used_eur_web": 4300,
    "lbc_comparables_count": 5,
    "lbc_comparables_median_eur": 4100,
    "lbc_comparables_samples": [...],
    "durations_s": {"extraction_s": 4.2, "web_s": 28.1, "lbc_s": 1.4, "synth_s": 12.3, "total_s": 46.0},
    "models": {"extract": "llama3.2:3b", "synth": "mistral:7b"}
  }
}
```

Les cles `ad_*` et `asking_price_eur` apportent la tracabilite. Les cles centrales (brand → cons) suivent exactement le schema utilise par [lbc-sniper](../lbc-sniper). `_sources` est purement informatif.

## Structure

```
.
├── enrich_bike.py            # agent + CLI principal
├── benchmark_extraction.py   # benchmark d'extraction sur multiples modeles
├── data/                     # fixtures
│   ├── annonces.json
│   ├── catalogue.json
│   └── expected.json
├── samples/                  # sorties d'exemples (gitignored)
└── .cache/                   # cache HTTP disque (gitignored)
```

## Benchmark d'extraction

`benchmark_extraction.py` compare plusieurs modeles Ollama sur les 23 annonces de `data/annonces.json`.

```powershell
python .\benchmark_extraction.py                       # tous les modeles
python .\benchmark_extraction.py --model mistral:7b    # un seul
python .\benchmark_extraction.py --details             # par-annonce
```

### Resultats actuels (extraction marque/modele/annee/taille/taille_roues)

| Modele | Score | Taux | Temps |
|---|---:|---:|---:|
| `gemma3:4b` | 90/115 | 78% | 37.4s |
| `mistral:7b` | 88/115 | 77% | 41.6s |
| `llama3.2:3b` | 85/115 | 74% | 41.0s |
| `qwen2.5:7b` | 81/115 | 70% | 57.7s |

### Scores par champ

```
marque       : 80/92 (87%)
modele       : 80/92 (87%)
annee        : 74/92 (80%)
taille       : 59/92 (64%)
taille_roues : 51/92 (55%)
```

## Choix techniques

- **Jina Reader** (`r.jina.ai/<url>`) pour les fetches de pages : contourne Cloudflare et anti-bot des sites constructeur, retourne du markdown propre. Avec cle API gratuite : 500 RPM.
- **Jina Search** (`s.jina.ai/?q=`) en backend search prioritaire si cle presente. Fallback DDG-via-Jina, puis DDG direct, puis Bing.
- **Cache disque** (`.cache/enrich_bike/`) sur toutes les requetes HTTP : ttl 7 jours, accelere les re-runs.
- **Throttle par domaine** : DDG 8s, Bing 6s, Jina 0.3s avec cle (3s sans), reste 0.3-0.8s.
- **Backoff exponentiel** sur 403/429 : 30s, 60s, 120s avec jitter.
- **URLs LBC filtrees** des resultats search : pas de doublon avec l'annonce source.
- **Mistral 7b pour la synthese** : raisonnement plus fiable que llama3.2:3b sur les pieges (modeles homonymes adulte/enfant, decote, classification VTT).
