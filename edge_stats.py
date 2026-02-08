#!/usr/bin/env python3

import argparse
import os
import sys
from collections import Counter

from include_analysis import ParseError, load_include_analysis


def main():
    parser = argparse.ArgumentParser(description="Output stats about edges in an include analysis")
    parser.add_argument(
        "include_analysis_output",
        type=str,
        nargs="?",
        help="The include analysis output to use (can be a file path or URL). If not specified, pulls the latest.",
    )
    parser.add_argument("--top", type=int, default=10, help="Number of top files to show (default: 10).")
    parser.add_argument("--verbose", action="store_true", default=False, help="Enable verbose logging.")
    args = parser.parse_args()

    try:
        include_analysis = load_include_analysis(args.include_analysis_output)
    except ParseError as e:
        message = str(e)
        print("error: Could not parse include analysis output file")
        if message:
            print(message)
        return 2

    roots = set(include_analysis["roots"])
    root_count = len(roots)

    # Count total edges
    total_edges = sum(len(includes) for includes in include_analysis["includes"].values())
    total_edges_ex_gen = sum(
        len(includes) for includer, includes in include_analysis["includes"].items() if not includer.startswith("out/")
    )

    # Count edges directly out of roots and track which files roots include
    edges_from_roots = 0
    edges_from_roots_ex_gen = 0
    root_includes_counter = Counter()
    root_includes_counter_ex_gen = Counter()

    for root in roots:
        includes = include_analysis["includes"].get(root, [])
        edges_from_roots += len(includes)
        if not root.startswith("out/"):
            edges_from_roots_ex_gen += len(includes)
        for included in includes:
            root_includes_counter[included] += 1
            if not root.startswith("out/"):
                root_includes_counter_ex_gen[included] += 1

    # Count edges directly out of headers (non-roots)
    edges_from_headers = 0
    edges_from_headers_ex_gen = 0
    for includer, includes in include_analysis["includes"].items():
        if includer not in roots:
            edges_from_headers += len(includes)
            if not includer.startswith("out/"):
                edges_from_headers_ex_gen += len(includes)

    try:
        print(f"Total edges: {total_edges:,} ({total_edges_ex_gen:,} excluding generated including headers)")
        print(
            f"Edges directly out of roots: {edges_from_roots:,} ({edges_from_roots_ex_gen:,} excluding generated including headers)"
        )
        print(
            f"Edges directly out of headers: {edges_from_headers:,} ({edges_from_headers_ex_gen:,} excluding generated including headers)"
        )
        print()
        print(f"Top {args.top} Files Included By Roots:")

        for filename, count in root_includes_counter.most_common(args.top):
            prevalence = (100.0 * count) / root_count
            count_ex_gen = root_includes_counter_ex_gen.get(filename, 0)
            print(
                f"* {filename} {count:,} ({prevalence:.2f}% prevalence) ({count_ex_gen:,} excluding generated including headers)"
            )

        sys.stdout.flush()
    except BrokenPipeError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(1)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass  # Don't show the user anything
