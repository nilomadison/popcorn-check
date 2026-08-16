"""Batch / nightly sync entry point for Popcorn Check.

Modes:
  (default)   refresh catalog, then up to 150 RT enrichment attempts
  --catalog   refresh the YouTube TV catalog only
  --backfill N  run only N RT enrichment attempts (no catalog refresh)
  --all       refresh catalog + up to 2000 enrichment attempts
"""

from __future__ import annotations

import argparse

import ytv


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
        "--all", action="store_true", help="catalog refresh + 2000-title backfill"
    )
    args = parser.parse_args()

    if args.catalog:
        n = ytv.fetch_catalog()
        print(f"catalog refreshed: {n} titles")
        return

    if args.backfill is not None:
        stats = ytv.enrich(limit=args.backfill)
        print(f"backfill complete: {stats}")
        return

    if args.all:
        n = ytv.fetch_catalog()
        print(f"catalog refreshed: {n} titles")
        stats = ytv.enrich(limit=2000)
        print(f"enrichment complete: {stats}")
        return

    # Default nightly: refresh catalog, then attempt up to 150 enrichments.
    n = ytv.fetch_catalog()
    print(f"catalog refreshed: {n} titles")
    stats = ytv.enrich(limit=150)
    print(f"enrichment complete: {stats}")


if __name__ == "__main__":
    main()
