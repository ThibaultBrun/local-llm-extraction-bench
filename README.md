# Local LLM Extraction Bench

Benchmark d'extraction structuree avec des LLM locaux via Ollama.

Le projet compare plusieurs modeles IA locaux sur un jeu d'annonces et calcule un taux de reussite sur cinq champs simples : `marque`, `modele`, `annee`, `taille`, `taille_roues`.

## Fichiers

- `benchmark_extraction.py` : script principal du benchmark.
- `annonces.json` : annonces a analyser.
- `expected.json` : resultats attendus pour comparer les extractions.
- `catalogue.json` : petit catalogue de modeles pour le post-traitement.
- `requirements.txt` : dependances Python.

## Prerequis

- Python 3
- Ollama installe et lance
- Au moins un modele Ollama disponible

Exemples de modeles utiles :

```powershell
ollama pull llama3.2:3b
ollama pull qwen2.5:7b
ollama pull mistral:7b
ollama pull gemma3:4b
```

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Utilisation

Benchmark resume :

```powershell
python .\benchmark_extraction.py
```

Avec details par annonce :

```powershell
python .\benchmark_extraction.py --details
```

Tester un seul modele :

```powershell
python .\benchmark_extraction.py --model llama3.2:3b
```

Limiter le nombre d'annonces :

```powershell
python .\benchmark_extraction.py --limit 5
```

Combiner les options :

```powershell
python .\benchmark_extraction.py --model qwen2.5:7b --limit 5 --details
```

Changer le timeout par appel Ollama :

```powershell
python .\benchmark_extraction.py --timeout 30
```

## Benchmark

Le benchmark actuel compare 23 annonces, soit 115 champs par modele.

Chaque appel Ollama a un timeout de 30 secondes par defaut. Si un modele depasse ce delai ou renvoie un JSON invalide, l'annonce est comptee comme KO pour ce modele.

La sortie finale affiche aussi les taux de reussite par champ, globalement et pour chaque modele.

Pour la taille du cadre, le post-traitement utilise `attributes.bicycle_size` en priorite quand l'information existe, puis cherche dans le titre et la description seulement en fallback.

Pour la taille des roues, le post-traitement applique la meme logique avec `attributes.bicycle_wheel_size`, puis normalise la valeur sans unite.

## Resultats precedents

Benchmark sur 23 annonces avec extraction de `marque`, `modele`, `annee` et `taille`.

Ces resultats datent du benchmark a 4 champs, avant l'ajout de `taille_roues`.

| Modele | Score | Taux | Temps |
| --- | ---: | ---: | ---: |
| `llama3.2:3b` | 70/92 | 76% | 33.17s |
| `gemma3:4b` | 69/92 | 75% | 32.46s |
| `mistral:7b` | 68/92 | 74% | 37.02s |
| `qwen2.5:7b` | 65/92 | 71% | 49.79s |

Ces resultats mesurent uniquement l'identification directe depuis l'annonce. Les caracteristiques techniques detaillees peuvent ensuite etre enrichies depuis des sources specialisees une fois le modele identifie.
