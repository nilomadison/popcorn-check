"""Command-line Rotten Tomatoes lookup for Popcorn Check.

Usage: python pc.py "The Godfather" [--year 1972]
"""

from __future__ import annotations

import argparse
import json

import rt


def main() -> None:
    parser = argparse.ArgumentParser(description="Rotten Tomatoes lookup")
    parser.add_argument("title", help="movie title to look up")
    parser.add_argument("--year", type=int, default=None, help="release year")
    parser.add_argument(
        "--search", action="store_true", help="show raw search candidates"
    )
    args = parser.parse_args()

    if args.search:
        print(json.dumps(rt.search(args.title), indent=2))
        return

    result = rt.lookup(args.title, args.year)
    if result is None:
        print("no match found")
        return
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
