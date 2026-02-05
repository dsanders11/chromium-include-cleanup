#!/usr/bin/env python3

import argparse
import csv
import logging
import os
import sys

import networkx as nx

from include_analysis import IncludeAnalysisOutput, ParseError, load_include_analysis
from typing import Optional, Set, Tuple
from utils import create_graph_from_include_analysis


def create_include_graph(
    include_analysis: IncludeAnalysisOutput,
    skips: Optional[Tuple[Tuple[str, str]]],
) -> nx.DiGraph:
    DG: nx.DiGraph = create_graph_from_include_analysis(include_analysis)

    for includer, included in skips:
        if includer in include_analysis["files"] and included in include_analysis["files"]:
            includer_idx = include_analysis["files"].index(includer)
            included_idx = include_analysis["files"].index(included)

            if DG.has_edge(includer_idx, included_idx):
                DG.remove_edge(includer_idx, included_idx)
            else:
                logging.warning(f"Skip edge {includer} -> {included} not found in include graph")

    return DG


def count_reachable_roots(
    include_analysis: IncludeAnalysisOutput,
    DG: nx.DiGraph,
    target: str,
) -> int:
    files = include_analysis["files"]
    target_node = files.index(target)

    reachable_nodes = set(
        [files[idx] for idx in nx.dfs_postorder_nodes(DG.reverse(), source=target_node) if idx != target_node]
    )

    reachable_roots = set(include_analysis["roots"]).intersection(reachable_nodes)

    return len(reachable_roots)


def main():
    parser = argparse.ArgumentParser(description="Count the number of roots reachable from target.")
    parser.add_argument(
        "include_analysis_output",
        type=str,
        nargs="?",
        help="The include analysis output to use (can be a file path or URL). If not specified, pulls the latest.",
    )
    parser.add_argument("target", help="Target file.")
    parser.add_argument("--skips", action="append", default=[], help="Edges to remove from the graph.")
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

    for target in args.target.split(","):
        if target not in include_analysis["files"]:
            print(f"error: {target} is not a known file")
            return 1

    skips: Set[Tuple[str, str]] = set()

    for skips_file in args.skips:
        with open(skips_file, "r", newline="") as f:
            skips.update(
                [tuple(row) for row in csv.reader(f) if row and row[0].strip() and not row[0].startswith("#")]
            )

    DG = create_include_graph(include_analysis, tuple(skips))

    try:
        reachable_roots = count_reachable_roots(include_analysis, DG, args.target)
        print(reachable_roots)
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
