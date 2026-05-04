# Local LLM Extraction Bench

Benchmark d'extraction structuree avec des LLM locaux via Ollama.

Le projet compare plusieurs modeles IA locaux sur un jeu d'annonces et calcule un taux de reussite champ par champ a partir de resultats attendus.

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

## Resultats actuels

Benchmark sur 23 annonces, soit 322 champs compares par modele.

| Modele | Score | Taux | Temps |
| --- | ---: | ---: | ---: |
| `llama3.2:3b` | 160/322 | 50% | 98.72s |
| `gemma3:4b` | 150/322 | 47% | 79.72s |
| `mistral:7b` | 149/322 | 46% | 2098.61s |
| `qwen2.5:7b` | 148/322 | 46% | 151.66s |

Ces premiers resultats sont volontairement stricts : chaque champ doit correspondre a la valeur attendue apres normalisation simple. Ils montrent surtout que le prompt et le post-traitement doivent encore etre ameliores avant d'obtenir une extraction fiable.
