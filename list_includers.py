#!/usr/bin/env python3

import argparse
import csv
import logging
import os
import sys
import typing

from common import IgnoresConfiguration, IncludeChange
from filter_include_changes import filter_changes
from include_analysis import IncludeAnalysisOutput, ParseError, parse_raw_include_analysis_output
from typing import Dict, Iterator, Tuple
from utils import (
    get_include_analysis_edges_centrality,
    get_include_analysis_edge_expanded_sizes,
    get_include_analysis_edge_file_sizes,
    get_include_analysis_edge_includer_size,
    get_include_analysis_edge_prevalence,
    get_include_analysis_edge_sizes,
    load_config,
    normalize_include_path,
)


def list_includers(
    include_analysis: IncludeAnalysisOutput,
    filename: str,
    metric: str = None,
    transitive=False,
    changes_file: typing.TextIO = None,
    ignores: IgnoresConfiguration = None,
    filter_generated_files=True,
    filter_mojom_headers=True,
    filter_third_party=False,
) -> Iterator[Tuple[str, str, int]]:
    root_count = len(include_analysis["roots"])
    edges = set()
    unused_edges = set()
    include_changes = None

    if changes_file:
        include_changes = filter_changes(
            csv.reader(changes_file),
            ignores=ignores,
            change_type_filter=IncludeChange.REMOVE,
            filter_generated_files=filter_generated_files,
            filter_mojom_headers=filter_mojom_headers,
            filter_third_party=filter_third_party,
        )

        for change_type_value, _, includer, included, *_ in include_changes:
            included = normalize_include_path(
                include_analysis, includer, included, include_directories=include_directories
            )
            unused_edges.add((includer, included))

    def expand_includer(includer, included):
        if includer.startswith("third_party/libc++/src/include/"):
            return

        if (includer, included) in edges:
            return

        edges.add((includer, included))

        if includer in include_analysis["included_by"]:
            for transitive_includer in include_analysis["included_by"][includer]:
                expand_includer(transitive_includer, includer)

    for includer in include_analysis["included_by"][filename]:
        if transitive:
            expand_includer(includer, filename)
        else:
            edges.add((includer, filename))

    if metric == "input_size":
        edge_weights = get_include_analysis_edge_sizes(include_analysis)
    elif metric == "expanded_size":
        edge_weights = get_include_analysis_edge_expanded_sizes(include_analysis)
    elif metric == "file_size":
        edge_weights = get_include_analysis_edge_file_sizes(include_analysis)
    elif metric == "centrality":
        edge_weights = get_include_analysis_edges_centrality(include_analysis)
    elif metric == "prevalence":
        edge_weights = get_include_analysis_edge_prevalence(include_analysis)
    elif metric == "includer_size":
        edge_weights = get_include_analysis_edge_includer_size(include_analysis)

    for includer, included in edges:
        # If include changes are provided, skip edges which are not unused
        if include_changes and (includer, included) not in unused_edges:
            continue

        weight = edge_weights[includer][included] if metric else None

        yield (includer, included, weight)


def main():
    parser = argparse.ArgumentParser(description="List includers of a file")
    parser.add_argument(
        "include_analysis_output",
        type=argparse.FileType("r"),
        help="The include analysis output to use.",
    )
    parser.add_argument("filename", help="File to list includers for.")
    parser.add_argument("--config", help="Name of config file to use.")
    parser.add_argument("--transitive", action="store_true", help="List all transitive includers.")
    parser.add_argument(
        "--include-changes",
        type=argparse.FileType("r"),
        help="CSV of include changes to filter.",
    )
    parser.add_argument(
        "--metric",
        choices=["centrality", "expanded_size", "file_size", "includer_size", "input_size", "prevalence"],
        default="prevalence",
        help="Metric to use for edge weights.",
    )
    parser.add_argument(
        "--filter-third-party", action="store_true", help="Filter out third_party/ (excluding blink) and v8."
    )
    parser.add_argument("--no-filter-generated-files", action="store_true", help="Don't filter out generated files.")
    parser.add_argument("--no-filter-mojom-headers", action="store_true", help="Don't filter out mojom headers.")
    parser.add_argument("--no-filter-ignores", action="store_true", help="Don't filter out ignores.")
    parser.add_argument("--verbose", action="store_true", default=False, help="Enable verbose logging.")
    args = parser.parse_args()

    try:
        include_analysis = parse_raw_include_analysis_output(args.include_analysis_output.read())
    except ParseError as e:
        message = str(e)
        print("error: Could not parse include analysis output file")
        if message:
            print(message)
        return 2

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    config = None
    ignores = None

    if args.config:
        config = load_config(args.config)

    if config and not args.no_filter_ignores:
        ignores = config.ignores

    csv_writer = csv.writer(sys.stdout)

    if args.filename not in include_analysis["files"]:
        print(f"error: {args.filename} is not a known file")
        return 1

    try:
        for row in list_includers(
            include_analysis,
            args.filename,
            args.metric,
            transitive=args.transitive,
            changes_file=args.include_changes,
            ignores=ignores,
            filter_generated_files=not args.no_filter_generated_files,
            filter_mojom_headers=not args.no_filter_mojom_headers,
            filter_third_party=args.filter_third_party,
            include_directories=config.includeDirs if config else None,
        ):
            csv_writer.writerow(row)

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
