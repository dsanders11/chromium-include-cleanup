#!/usr/bin/env python3

import argparse
import csv
import logging
import os
import re
import sys
import urllib.request
from datetime import datetime

from include_analysis import IncludeAnalysisOutput, ParseError, parse_raw_include_analysis_output
from suggest_include_changes import filter_filenames
from utils import (
    get_include_analysis_edge_expanded_sizes,
    get_include_analysis_edge_prevalence,
    get_include_analysis_edges_centrality,
    get_latest_include_analysis,
)


def extract_include_analysis_edges(
    include_analysis: IncludeAnalysisOutput,
    weight_threshold=None,
    filter_generated_files=False,
    filter_mojom_headers=False,
    filter_third_party=False,
):
    filenames = filter_filenames(
        include_analysis["files"],
        filter_generated_files=filter_generated_files,
        filter_mojom_headers=filter_mojom_headers,
        filter_third_party=filter_third_party,
    )

    expanded_size_edge_weights = get_include_analysis_edge_expanded_sizes(include_analysis)
    prevalence_edge_weights = get_include_analysis_edge_prevalence(include_analysis)
    centrality_edge_weights = get_include_analysis_edges_centrality(include_analysis)

    for file in filenames:
        for [include, size] in include_analysis["esizes"][file].items():
            if weight_threshold and float(size) < weight_threshold:
                continue

            prevalence = prevalence_edge_weights[file][include]
            expanded_size = expanded_size_edge_weights[file][include]
            centrality = centrality_edge_weights[file][include]

            yield file, include, size, prevalence, expanded_size, centrality


def main():
    parser = argparse.ArgumentParser(description="Extract include edges from include analysis, with filtering")
    parser.add_argument(
        "include_analysis_output",
        type=argparse.FileType("r"),
        nargs="?",
        help="The include analysis output to use.",
    )
    parser.add_argument(
        "--weight-threshold", type=float, help="Filter out changes with a weight value below the threshold."
    )
    parser.add_argument(
        "--filter-third-party", action="store_true", help="Filter out third_party/ (excluding blink) and v8."
    )
    parser.add_argument("--filter-generated-files", action="store_true", help="Filter out generated files.")
    parser.add_argument("--filter-mojom-headers", action="store_true", help="Filter out mojom headers.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--quiet", action="store_true", default=False, help="Only log warnings and errors.")
    group.add_argument("--verbose", action="store_true", default=False, help="Enable verbose logging.")
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(message)s",
        level=logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO,
    )

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

    csv_writer = csv.writer(sys.stdout)

    try:
        for row in extract_include_analysis_edges(
            include_analysis,
            weight_threshold=args.weight_threshold,
            filter_generated_files=args.filter_generated_files,
            filter_mojom_headers=args.filter_mojom_headers,
            filter_third_party=args.filter_third_party,
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
