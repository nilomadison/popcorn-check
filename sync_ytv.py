"""Batch / nightly sync entry point for Popcorn Check.

Modes:
  (default)   refresh provider catalogs, then up to 500 RT enrichment attempts
  --catalog   refresh provider catalogs only
  --backfill N  run only N RT enrichment attempts (no catalog refresh)
  --revalidate-rt  audit stored RT URLs and quarantine identity mismatches
  --tmdb-backfill N  run only N TMDb enrichment attempts
  --all       refresh catalog + up to 2000 enrichment attempts
"""

from __future__ import annotations

import argparse

import tmdb
import ytv


def refresh_catalogs() -> dict[str, int]:
    counts = {}
    for provider in ytv.PROVIDERS:
        counts[provider] = ytv.fetch_catalog(provider)
        print(f"{provider} catalog refreshed: {counts[provider]} titles")
    return counts


def print_tmdb_result(stats: dict[str, int]) -> None:
    print(f"TMDb enrichment complete: {stats}")
    print(f"TMDb HTTP 429 responses retried: {tmdb.rate_limit_retry_count()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Popcorn Check data sync")
    parser.add_argument(
        "--catalog", action="store_true", help="refresh catalog only"
    )
    parser.add_argument(
        "--backfill", type=int, default=None, metavar="N",
        help="run only N RT enrichment attempts (no catalog refresh)",
    )
    parser.add_argument(
        "--revalidate-rt", action="store_true",
        help="revalidate active stored RT URLs and quarantine identity mismatches",
    )
    parser.add_argument(
        "--tmdb-backfill", type=int, default=None, metavar="N",
        help="run only N TMDb enrichment attempts (no catalog refresh)",
    )
    parser.add_argument(
        "--all", action="store_true", help="catalog refresh + 2000-title backfill"
    )
    args = parser.parse_args()

    if args.catalog:
        refresh_catalogs()
        return

    if args.backfill is not None:
        stats = ytv.enrich(limit=args.backfill)
        print(f"backfill complete: {stats}")
        return

    if args.revalidate_rt:
        stats = ytv.revalidate_ratings()
        print(f"RT revalidation complete: {stats}")
        return

    if args.tmdb_backfill is not None:
        stats = ytv.enrich_tmdb(limit=args.tmdb_backfill)
        print_tmdb_result(stats)
        return

    if args.all:
        refresh_catalogs()
        tmdb_stats = ytv.enrich_tmdb(limit=5000)
        print_tmdb_result(tmdb_stats)
        stats = ytv.enrich(limit=2000)
        print(f"enrichment complete: {stats}")
        return

    # Default nightly: refresh catalog, then attempt up to 500 enrichments.
    refresh_catalogs()
    tmdb_stats = ytv.enrich_tmdb(limit=250)
    print_tmdb_result(tmdb_stats)
    stats = ytv.enrich(limit=500)
    print(f"enrichment complete: {stats}")


if __name__ == "__main__":
    main()
