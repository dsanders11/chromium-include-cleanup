#!/usr/bin/env python3

import argparse
import csv
import logging
import os
import sys

import networkx as nx

from include_analysis import IncludeAnalysisOutput, ParseError, parse_raw_include_analysis_output
from typing import Iterator, Tuple
from utils import (
    create_graph_from_include_analysis,
    get_latest_include_analysis,
)


def minimum_edge_cut(
    include_analysis: IncludeAnalysisOutput,
    source: str,
    target: str,
) -> Iterator[Tuple[str, str]]:
    files = include_analysis["files"]
    DG: nx.DiGraph = create_graph_from_include_analysis(include_analysis)

    edge_cut = nx.minimum_edge_cut(DG, files.index(source), files.index(target))

    for includer_idx, include_idx in edge_cut:
        includer = files[includer_idx]
        included = files[include_idx]

        yield includer, included


def main():
    parser = argparse.ArgumentParser(description="Find the minimum edge cut between two files")
    parser.add_argument(
        "include_analysis_output",
        type=argparse.FileType("r"),
        nargs="?",
        help="The include analysis output to use.",
    )
    parser.add_argument("source", help="Source file.")
    parser.add_argument("target", help="Target file.")
    parser.add_argument("--verbose", action="store_true", default=False, help="Enable verbose logging.")
    args = parser.parse_args()

    # If the user specified an include analysis output file, use that instead of fetching it
    if args.include_analysis_output:
        raw_include_analysis = args.include_analysis_output.read()
    else:
        raw_include_analysis = get_latest_include_analysis()

    try:
        include_analysis = parse_raw_include_analysis_output(raw_include_analysis)
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
