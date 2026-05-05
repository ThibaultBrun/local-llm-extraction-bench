"""CLI entry point for the bike-ia-agent."""

import argparse
import json
import sys
from pathlib import Path

from bike_agent import config
from bike_agent.lbc import fetch_lbc_ad, fetch_lbc_ad_by_id
from bike_agent.pipeline import enrich_ad


def parse_args():
    parser = argparse.ArgumentParser(description="Agent d'identification, recherche web et evaluation marche pour velos.")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--annonce", help="Cle de l'annonce dans le fichier JSON local (mode test legacy).")
    src.add_argument("--ad-json", help="Chemin d'un JSON d'annonce LBC-style (id, subject, body, price, url, city, attributes).")
    src.add_argument("--lbc-search", help="Query Leboncoin pour fetch des annonces et les enrichir.")
    src.add_argument("--lbc-id", help="ID d'une annonce Leboncoin (extrait directement par id, sans search).")
    parser.add_argument("--annonces", default="data/annonces.json", help="Fichier JSON des annonces (mode --annonce).")
    parser.add_argument("--lbc-limit", type=int, default=3, help="Nombre d'annonces a fetcher en mode --lbc-search.")
    parser.add_argument("--no-lbc-comparables", action="store_true", help="Desactive la recherche d'annonces LBC similaires pour le marche.")
    parser.add_argument("--domain", help="Domaine indicatif (vtt_enduro, vtt_dh, etc.) passe a la synthese.")
    parser.add_argument("--model", default="llama3.2:3b", help="Modele Ollama utilise pour identifier le velo et trier les sources web.")
    parser.add_argument("--synth-model", default="mistral:7b", help="Modele Ollama utilise pour la synthese finale (eval marche + deal score).")
    parser.add_argument("--no-synth", action="store_true", help="Desactive l'etape de synthese finale.")
    parser.add_argument("--synth-timeout", type=float, default=60, help="Timeout Ollama pour l'appel de synthese (modele plus gros = plus lent).")
    parser.add_argument("--http-timeout", type=float, default=10, help="Timeout HTTP en secondes.")
    parser.add_argument("--ollama-timeout", type=float, default=25, help="Timeout Ollama en secondes.")
    parser.add_argument("--max-results", type=int, default=6, help="Nombre de resultats web par requete.")
    parser.add_argument("--fetch-pages", action="store_true", help="Ouvre les pages trouvees pour chercher des prix.")
    parser.add_argument("--verbose", action="store_true", help="Affiche les recherches et sites fouilles en temps reel.")
    parser.add_argument("--delay-min", type=float, default=0.3, help="Latence minimum entre deux requetes HTTP sur un meme domaine non-sensible.")
    parser.add_argument("--delay-max", type=float, default=0.8, help="Latence maximum entre deux requetes HTTP sur un meme domaine non-sensible.")
    parser.add_argument("--retries", type=int, default=2, help="Nombre de retries doux sur HTTP 403/429.")
    parser.add_argument("--top-sources", type=int, default=8, help="Nombre de resultats retenus par Ollama apres tri.")
    parser.add_argument("--no-cache", action="store_true", help="Ignore le cache disque des requetes HTTP.")
    parser.add_argument("--raw", action="store_true", help="Sortie verbeuse (forme {payload, meta}) au lieu de la forme aplatie Claude-compatible.")
    parser.add_argument("--output", help="Fichier de sortie JSON. Par defaut, affiche dans le terminal.")
    return parser.parse_args()


def load_annonce(path, key):
    annonces = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if key not in annonces:
        available = ", ".join(annonces.keys())
        raise SystemExit(f"Annonce introuvable: {key}. Disponibles: {available}")
    return annonces[key]


def _enrich_ad_with_args(ad, args):
    return enrich_ad(
        ad,
        domain_hint=args.domain,
        extract_model=args.model,
        synth_model=args.synth_model,
        fetch_pages=args.fetch_pages,
        fetch_lbc=not args.no_lbc_comparables,
        top_sources=args.top_sources,
        max_results=args.max_results,
        http_timeout=args.http_timeout,
        ollama_timeout=args.ollama_timeout,
        synth_timeout=args.synth_timeout,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        retries=args.retries,
        use_cache=not args.no_cache,
        verbose=args.verbose,
    )


def flatten_result(result):
    """Aplatit {payload, meta} en un seul dict :
    - top-level : ad_id, ad_url, ad_subject, asking_price_eur (tracabilite)
    - puis les cles du payload Claude-compatible (brand, model, ...)
    - puis _sources (signaux web + comparables LBC + durations) pour debug
    """
    payload = result.get("payload") or {}
    meta = result.get("meta") or {}
    durations = meta.get("durations") or {}
    flat = {
        "ad_id": meta.get("ad_id"),
        "ad_url": meta.get("ad_url"),
        "ad_subject": meta.get("ad_subject"),
        "asking_price_eur": meta.get("asking_price_eur"),
        "duration_s": durations.get("total_s"),
        "web_search_duration_s": durations.get("web_s"),
    }
    flat.update(payload)
    web_summary = meta.get("web_summary") or {}
    flat["_sources"] = {
        "extracted_identity": meta.get("identity"),
        "msrp_eur_web": web_summary.get("msrp_eur"),
        "retail_eur_web": web_summary.get("retail_eur_web"),
        "used_eur_web": web_summary.get("used_eur_web"),
        "msrp_samples": web_summary.get("msrp_samples"),
        "retail_samples": web_summary.get("retail_samples"),
        "web_candidates_count": web_summary.get("candidates_count"),
        "web_selected_count": web_summary.get("selected_count"),
        "lbc_comparables_count": (meta.get("lbc_comparables") or {}).get("count"),
        "lbc_comparables_median_eur": (meta.get("lbc_comparables") or {}).get("median_eur"),
        "lbc_comparables_samples": (meta.get("lbc_comparables") or {}).get("samples"),
        "durations_s": meta.get("durations"),
        "models": meta.get("models"),
        "synth_error": meta.get("synth_error"),
    }
    return flat


def _output(result_or_list, output_path, raw=False):
    if raw:
        rendered = json.dumps(result_or_list, ensure_ascii=False, indent=2)
    elif isinstance(result_or_list, list):
        flat = [flatten_result(r) for r in result_or_list]
        rendered = json.dumps(flat, ensure_ascii=False, indent=2)
    else:
        flat = flatten_result(result_or_list)
        rendered = json.dumps(flat, ensure_ascii=False, indent=2)
    if output_path:
        Path(output_path).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


def main():
    args = parse_args()
    config.CACHE_ENABLED = not args.no_cache

    if args.no_synth:
        print("[warn] --no-synth ignore en mode pipeline complet (la synthese est integree).", file=sys.stderr)

    if args.annonce:
        annonce_text = load_annonce(args.annonces, args.annonce)
        ad = {"id": None, "subject": args.annonce, "body": annonce_text}
        result = _enrich_ad_with_args(ad, args)
        _output(result, args.output, raw=args.raw)
        return

    if args.ad_json:
        ad = json.loads(Path(args.ad_json).read_text(encoding="utf-8"))
        result = _enrich_ad_with_args(ad, args)
        _output(result, args.output, raw=args.raw)
        return

    if args.lbc_id:
        ad = fetch_lbc_ad_by_id(args.lbc_id, verbose=args.verbose)
        if not ad:
            print(f"[lbc] Annonce id={args.lbc_id} introuvable.", file=sys.stderr)
            sys.exit(1)
        print(f"\n=== {ad.get('subject', '')[:80]} ===", file=sys.stderr)
        result = _enrich_ad_with_args(ad, args)
        _output(result, args.output, raw=args.raw)
        return

    if args.lbc_search:
        ads = fetch_lbc_ad(args.lbc_search, limit=args.lbc_limit, verbose=args.verbose)
        if not ads:
            print("[lbc] Aucune annonce trouvee.", file=sys.stderr)
            sys.exit(1)
        results = []
        for idx, ad in enumerate(ads, 1):
            print(f"\n=== [{idx}/{len(ads)}] {ad.get('subject', '')[:80]} ===", file=sys.stderr)
            try:
                results.append(_enrich_ad_with_args(ad, args))
            except Exception as exc:
                print(f"[error] ad {ad.get('id')}: {exc}", file=sys.stderr)
                results.append({"payload": None, "meta": {"ad_id": ad.get("id"), "error": str(exc)}})
        _output(results, args.output, raw=args.raw)
        return
