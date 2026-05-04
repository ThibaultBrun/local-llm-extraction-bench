import argparse
import json
from pathlib import Path
import re
import time
import unicodedata

import ollama


KNOWN_BRANDS = ["Orbea", "Commencal"]
ALLOWED_WHEEL_SIZES = {"12", "14", "16", "18", "20", "24", "26", "27.5", "28", "29"}
INTERESTING_MODEL_ORDER = [
    "qwen2.5:7b",
    "mistral:7b",
    "gemma3:4b",
    "llama3.1:8b",
    "llama3.2:3b",
    "phi4-mini:latest",
    "qwen2.5:3b",
]
CATALOGUE = json.loads(Path("catalogue.json").read_text(encoding="utf-8"))


def normalize_text(value):
    return value.lower().replace('"', " pouces")


def annonce_text(annonce):
    if isinstance(annonce, str):
        return annonce

    parts = []
    if annonce.get("subject"):
        parts.append(f"Titre original\n{annonce['subject']}")
    if annonce.get("body"):
        parts.append(f"Description complete\n{annonce['body']}")
    return "\n\n".join(parts)


def annonce_attributes(annonce):
    if isinstance(annonce, dict):
        return annonce.get("attributes") or {}
    return {}


def render_annonce(annonce):
    text = annonce_text(annonce)
    attributes = annonce_attributes(annonce)
    if not attributes:
        return text

    return f"{text}\n\nAttributs Leboncoin\n{json.dumps(attributes, ensure_ascii=False)}"


def find_brand(annonce):
    text = normalize_text(annonce_text(annonce))
    for brand in KNOWN_BRANDS:
        if brand.lower() in text:
            return brand
    return None


def find_attribute_frame_size(annonce):
    frame_size = annonce_attributes(annonce).get("bicycle_size")
    if not frame_size:
        return None
    return str(frame_size).upper()


def find_attribute_wheel_size(annonce):
    wheel_size = annonce_attributes(annonce).get("bicycle_wheel_size")
    if not wheel_size:
        return None

    match = re.search(r"(?<!\d)(\d{2}(?:[.,]\d)?)", str(wheel_size))
    if not match:
        return None

    normalized = match.group(1).replace(",", ".")
    if normalized not in ALLOWED_WHEEL_SIZES:
        return None
    return normalized


def find_frame_size(annonce):
    match = re.search(
        r"\b(?:taille|cadre|en)\s+(XS|S|M|L|XL|XXL)\b",
        annonce_text(annonce),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return match.group(1).upper()


def find_known_model(annonce):
    text = normalize_text(annonce_text(annonce))

    for item in CATALOGUE["modeles"]:
        if item["marque"].lower() not in text:
            continue

        if not any(alias.lower() in text for alias in item["aliases"]):
            continue

        version = None
        for candidate in item["versions"]:
            if candidate.lower() in text:
                version = candidate
                break

        return item["modele"], version

    return None, None


def find_year(annonce):
    matches = re.findall(r"\b(20[0-3]\d)\b", annonce_text(annonce))
    if not matches:
        return None
    return int(matches[0])


def find_wheel_size(annonce):
    text = normalize_text(annonce_text(annonce))
    match = re.search(r"(?<!\d)(\d{2}(?:[.,]\d)?)\s*pouces\b", text)
    if not match:
        return None
    wheel_size = match.group(1).replace(",", ".")
    if wheel_size not in ALLOWED_WHEEL_SIZES:
        return None
    return wheel_size


def find_declared_state(annonce):
    text = normalize_text(annonce_text(annonce))
    if "quasiment neuf" in text:
        return "quasiment neuf"
    if "excellent etat" in text:
        return "excellent etat"
    if "tres bon etat" in text:
        return "tres bon etat"
    if re.search(r"\bneuf\b", text):
        return "neuf"
    return None


def post_process(data, annonce):
    text = annonce_text(annonce)
    normalized_annonce = normalize_text(text)

    brand = find_brand(annonce)
    if brand:
        data["marque"] = brand

    model, version = find_known_model(annonce)
    if model:
        data["modele"] = model
    if version:
        data["version"] = version

    year = find_year(annonce)
    if year:
        data["annee"] = year

    frame_size = find_attribute_frame_size(annonce) or find_frame_size(annonce)
    if frame_size:
        data["taille"] = frame_size

    wheel_size = find_attribute_wheel_size(annonce) or find_wheel_size(annonce)
    if wheel_size:
        data["taille_roues"] = wheel_size
        if isinstance(data.get("modele"), str):
            data["modele"] = re.sub(rf"\s+{re.escape(wheel_size)}$", "", data["modele"]).strip()
        if data.get("version") == wheel_size:
            data["version"] = None

    if isinstance(data.get("taille"), str) and "pouce" in data["taille"].lower():
        data["taille"] = None
    if isinstance(data.get("taille"), str) and '"' in data["taille"]:
        data["taille"] = None
    if data.get("taille") == data.get("taille_roues"):
        data["taille"] = None

    if isinstance(data.get("version"), str) and "pouce" in data["version"].lower():
        data["version"] = None

    if re.search(r"\bfox\s+float\s+r\b", text, flags=re.IGNORECASE):
        data["amortisseur"] = "Fox Float R"

    if data.get("transmission") == "Derailleur arriere" and "derailleur" not in normalized_annonce:
        data["transmission"] = None

    declared_state = find_declared_state(annonce)
    if declared_state:
        data["etat_declare"] = declared_state

    return data


def normalize_for_compare(value):
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        without_accents = unicodedata.normalize("NFKD", value)
        without_accents = "".join(char for char in without_accents if not unicodedata.combining(char))
        return re.sub(r"\s+", " ", without_accents.strip().lower())
    return value


def compare_to_expected(data, expected):
    details = []
    correct = 0

    for field in FIELDS:
        actual_value = data.get(field)
        expected_value = expected.get(field)
        is_match = normalize_for_compare(actual_value) == normalize_for_compare(expected_value)
        if is_match:
            correct += 1

        details.append(
            {
                "field": field,
                "ok": is_match,
                "actual": actual_value,
                "expected": expected_value,
            }
        )

    total = len(FIELDS)
    return {
        "correct": correct,
        "total": total,
        "rate": correct / total if total else 0,
        "details": details,
    }


def print_score(score):
    print(f'\n--- Score: {score["correct"]}/{score["total"]} ({score["rate"]:.0%}) ---')
    errors = [detail for detail in score["details"] if not detail["ok"]]
    if not errors:
        print("Tout est correct.")
        return

    print("Erreurs:")
    for error in errors:
        print(
            f'- {error["field"]}: obtenu={json.dumps(error["actual"], ensure_ascii=False)} '
            f'attendu={json.dumps(error["expected"], ensure_ascii=False)}'
        )


def field_scores(results):
    scores = {field: {"correct": 0, "total": 0} for field in FIELDS}

    for result in results:
        details = result["score"]["details"]
        if details:
            for detail in details:
                scores[detail["field"]]["total"] += 1
                if detail["ok"]:
                    scores[detail["field"]]["correct"] += 1
        elif result["score"]["total"]:
            for field in FIELDS:
                scores[field]["total"] += 1

    return scores


def print_field_scores(results, title):
    print(f"\n=== {title} ===")
    scores = field_scores(results)

    for field in FIELDS:
        correct = scores[field]["correct"]
        total = scores[field]["total"]
        rate = correct / total if total else 0
        print(f"{field}: {correct}/{total} ({rate:.0%})")


FIELDS = [
    "marque",
    "modele",
    "annee",
    "taille",
    "taille_roues",
]

schema = {
    "type": "object",
    "properties": {
        "marque": {"type": ["string", "null"], "description": "Marque du velo, meme si elle apparait seulement dans le titre."},
        "modele": {"type": ["string", "null"], "description": "Nom commercial du modele, sans la marque."},
        "annee": {"type": ["integer", "null"], "description": "Annee du velo ou du modele si elle est explicitement presente dans l'annonce."},
        "taille": {"type": ["string", "null"], "description": "Taille du cadre, par exemple S, M, L, XL ou U. Ne jamais mettre la taille des roues ici."},
        "taille_roues": {"type": ["string", "null"], "description": "Diametre des roues sans unite. Valeurs autorisees : 12, 14, 16, 18, 20, 24, 26, 27.5, 28, 29."},
    },
    "required": FIELDS,
    "additionalProperties": False,
}

def build_prompt(annonce):
    return f"""
Extrais la marque, le modele, l'annee, la taille du cadre et la taille des roues de cette annonce de velo.

Regles :
- Retourne uniquement un objet JSON valide.
- Remplis toutes les cles du schema.
- Si une information n'est pas presente dans l'annonce, utilise null.
- N'invente pas d'information.
- Le modele est le nom commercial sans la marque.
- L'annee doit etre un entier.
- La taille correspond a la taille du cadre, par exemple S, M, L, XL ou U.
- La taille_roues correspond au diametre des roues.
- Pour taille_roues, retourne uniquement la valeur normalisee sans unite : 12, 14, 16, 18, 20, 24, 26, 27.5, 28 ou 29.
- Ne confonds pas une taille de roues ou une taille de cadre avec une annee.
- Ne confonds pas la taille du cadre avec la taille des roues.
- La marque apparait souvent au debut du titre ou de la description.

Annonce :
{render_annonce(annonce)}
"""


def get_installed_models():
    models = ollama.list().models
    return [model.model for model in models]


def select_models_to_test(requested_models=None):
    installed = get_installed_models()

    if requested_models:
        missing = [model for model in requested_models if model not in installed]
        if missing:
            raise SystemExit(f"Modeles non installes: {', '.join(missing)}")
        return requested_models

    selected = [model for model in INTERESTING_MODEL_ORDER if model in installed]

    if selected:
        return selected

    return installed


def extract_annonce(model, name, annonce, expected, show_details, timeout):
    start = time.time()
    client = ollama.Client(timeout=timeout)

    try:
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
    except Exception as e:
        duration = time.time() - start
        if show_details:
            print(f"\nErreur appel Ollama: {e}")
        return {
            "model": model,
            "annonce": name,
            "duration": duration,
            "ok": False,
            "score": {"correct": 0, "total": len(FIELDS), "rate": 0, "details": []},
            "error": str(e),
        }

    duration = time.time() - start

    try:
        if show_details:
            print(f"\n=== {model} / {name} ===")
            print(f"Duree: {duration:.2f}s\n")
            print("Sortie brute:")
            print(response["message"]["content"])

        data = json.loads(response["message"]["content"])
        data = post_process(data, annonce)
        if show_details:
            print("\n--- JSON parse OK ---")
            print(json.dumps(data, indent=2, ensure_ascii=False))

        if expected:
            score = compare_to_expected(data, expected)
            if show_details:
                print_score(score)
        else:
            score = {"correct": 0, "total": 0, "rate": 0, "details": []}
            if show_details:
                print("\n--- Score: non note, resultat attendu absent ---")
        return {
            "model": model,
            "annonce": name,
            "duration": duration,
            "ok": True,
            "score": score,
        }
    except json.JSONDecodeError as e:
        if show_details:
            print(f"\nErreur JSON: {e}")
        return {
            "model": model,
            "annonce": name,
            "duration": duration,
            "ok": False,
            "score": {"correct": 0, "total": len(FIELDS), "rate": 0, "details": []},
            "error": str(e),
        }


def parse_args():
    parser = argparse.ArgumentParser(description="Benchmark d'extraction avec Ollama.")
    parser.add_argument("--details", action="store_true", help="Affiche les JSON et les erreurs champ par champ.")
    parser.add_argument("--model", action="append", help="Modele Ollama a tester. Peut etre repete.")
    parser.add_argument("--limit", type=int, help="Nombre maximum d'annonces a tester.")
    parser.add_argument("--timeout", type=float, default=30, help="Timeout par appel Ollama en secondes.")
    parser.add_argument("--annonces", default="annonces.json", help="Fichier JSON des annonces.")
    parser.add_argument("--expected", default="expected.json", help="Fichier JSON des resultats attendus.")
    return parser.parse_args()


def load_json(path, encoding="utf-8"):
    return json.loads(Path(path).read_text(encoding=encoding))


def main():
    args = parse_args()
    annonces = load_json(args.annonces, encoding="utf-8-sig")
    expected = load_json(args.expected)

    if args.limit is not None:
        annonces = dict(list(annonces.items())[: args.limit])

    results = []
    models_to_test = select_models_to_test(args.model)

    print("Modeles testes:", ", ".join(models_to_test) if models_to_test else "aucun")

    total_runs = len(models_to_test) * len(annonces)
    current_run = 0

    for model in models_to_test:
        for name, annonce in annonces.items():
            current_run += 1
            percent = current_run / total_runs if total_runs else 0
            print(f"[{current_run}/{total_runs}] {percent:.0%} - {model} / {name}")
            results.append(
                extract_annonce(
                    model=model,
                    name=name,
                    annonce=annonce,
                    expected=expected.get(name),
                    show_details=args.details,
                    timeout=args.timeout,
                )
            )

    if results:
        print("\n=== Resume ===")
        for result in results:
            status = "OK" if result["ok"] else "JSON KO"
            score = result["score"]
            print(
                f'{result["model"]} / {result["annonce"]}: {status}, '
                f'{score["correct"]}/{score["total"]} ({score["rate"]:.0%}) en {result["duration"]:.2f}s'
            )

        print("\n=== Classement modeles ===")
        for model in models_to_test:
            model_results = [result for result in results if result["model"] == model]
            total_correct = sum(result["score"]["correct"] for result in model_results)
            total_fields = sum(result["score"]["total"] for result in model_results)
            total_duration = sum(result["duration"] for result in model_results)
            rate = total_correct / total_fields if total_fields else 0
            print(f"{model}: {total_correct}/{total_fields} ({rate:.0%}) en {total_duration:.2f}s")

        print_field_scores(results, "Scores par champ")

        print("\n=== Scores par champ et modele ===")
        for model in models_to_test:
            model_results = [result for result in results if result["model"] == model]
            print_field_scores(model_results, model)


if __name__ == "__main__":
    main()
