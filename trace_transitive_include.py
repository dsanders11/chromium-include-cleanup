#!/usr/bin/env python3

import argparse
import csv
import logging
import os
import sys
from itertools import pairwise

import networkx as nx

from include_analysis import IncludeAnalysisOutput, ParseError, load_include_analysis
from typing import Iterator, Tuple
from utils import create_graph_from_include_analysis


def trace_transitive_include(
    include_analysis: IncludeAnalysisOutput,
    filename: str,
    header: str,
) -> Iterator[Tuple[str, str]]:
    files = include_analysis["files"]
    DG: nx.DiGraph = create_graph_from_include_analysis(include_analysis)

    shortest_path = nx.astar_path(DG, files.index(filename), files.index(header))

    for includer_idx, include_idx in pairwise(shortest_path):
        yield files[includer_idx], files[include_idx]


def main():
    parser = argparse.ArgumentParser(description="Trace a transitive include from a source file")
    parser.add_argument(
        "include_analysis_output",
        type=str,
        nargs="?",
        help="The include analysis output to use.",
    )
    parser.add_argument("filename", help="File to start the trace from.")
    parser.add_argument("header", help="Target header to trace to.")
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

    if args.filename not in include_analysis["files"]:
        print(f"error: {args.filename} is not a known file")
        return 1

    if args.header not in include_analysis["files"]:
        print(f"error: {args.header} is not a known file")
        return 1

    try:
        for row in trace_transitive_include(
            include_analysis,
            args.filename,
            args.header,
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
