#!/usr/bin/env python3

import argparse
import csv
import logging
import os
import sys

import networkx as nx

from include_analysis import IncludeAnalysisOutput, ParseError, load_include_analysis
from typing import Iterator, Tuple
from utils import create_graph_from_include_analysis, get_include_analysis_edge_prevalence


def minimum_edge_cut(
    include_analysis: IncludeAnalysisOutput,
    source: str,
    target: str,
    start_from_source_includes=False,
    prevalence_threshold: float = None,
) -> Iterator[Tuple[str, str]]:
    files = include_analysis["files"]
    DG: nx.DiGraph = create_graph_from_include_analysis(include_analysis)

    edge_prevalence = get_include_analysis_edge_prevalence(include_analysis)

    sources = include_analysis["includes"][source] if start_from_source_includes else [source]
    cuts = []

    for source in sources:
        # With the start_from_source_includes option, the target could be a direct include
        if source == target:
            logging.warning(f"{target} is a direct include of {source}")
            continue

        edge_cut = nx.minimum_edge_cut(DG, files.index(source), files.index(target))

        for includer_idx, include_idx in edge_cut:
            includer = files[includer_idx]
            included = files[include_idx]

            if start_from_source_includes:
                cuts.append((includer, included))
            else:
                prevalence = edge_prevalence[includer][included]

                if prevalence_threshold and prevalence < prevalence_threshold:
                    continue

                yield includer, included, prevalence

    if start_from_source_includes:
        # Deduplicate cuts
        for includer, included in set(cuts):
            prevalence = edge_prevalence[includer][included]

            if prevalence_threshold and prevalence < prevalence_threshold:
                continue

            yield includer, included, prevalence


def main():
    parser = argparse.ArgumentParser(description="Find the minimum edge cut between two files")
    parser.add_argument(
        "include_analysis_output",
        type=str,
        nargs="?",
        help="The include analysis output to use (can be a file path or URL). If not specified, pulls the latest.",
    )
    parser.add_argument("source", help="Source file.")
    parser.add_argument("target", help="Target file.")
    parser.add_argument(
        "--start-from-source-includes",
        action="store_true",
        help="Start from includes of the source file, rather than the source file itself.",
    )
    parser.add_argument(
        "--prevalence-threshold", type=float, help="Filter out edges with a prevalence percentage below the threshold."
    )
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

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    csv_writer = csv.writer(sys.stdout)

    if args.source not in include_analysis["files"]:
        print(f"error: {args.source} is not a known file")
        return 1

    if args.target not in include_analysis["files"]:
        print(f"error: {args.target} is not a known file")
        return 1

    try:
        for row in minimum_edge_cut(
            include_analysis,
            args.source,
            args.target,
            start_from_source_includes=args.start_from_source_includes,
            prevalence_threshold=args.prevalence_threshold,
        ):
            csv_writer.writerow(row)

        sys.stdout.flush()
    except nx.exception.NetworkXNoPath:
        print(f"error: no transitive include path from {args.filename} to {args.header}")
        return 3
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
