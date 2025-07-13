#!/usr/bin/env python3

import argparse
import csv
import logging
import os
import sys
from collections import defaultdict

from common import FilteredIncludeChangeList, IgnoresConfiguration, IncludeChange
from filter_include_changes import Change, filter_changes
from include_analysis import IncludeAnalysisOutput, ParseError, parse_raw_include_analysis_output
from typing import Dict, Iterator, List, Tuple
from utils import (
    get_include_analysis_edges_centrality,
    get_include_analysis_edge_expanded_sizes,
    get_include_analysis_edge_file_sizes,
    get_include_analysis_edge_includer_size,
    get_include_analysis_edge_prevalence,
    get_include_analysis_edge_sizes,
    get_include_file_size,
    load_config,
    normalize_include_path,
)


def list_transitive_includes(
    include_analysis: IncludeAnalysisOutput,
    filename: str,
    metric: str = None,
    changes: List[Change] = None,
    ignores: IgnoresConfiguration = None,
    ignore_edge: Tuple[str, str] = None,
    filter_generated_files=False,
    filter_mojom_headers=False,
    filter_third_party=False,
    header_mappings: Dict[str, str] = None,
    include_directories: List[str] = None,
    apply_changes=False,
    remove_only=False,
    full=False,
) -> Iterator[Tuple[str, str, int]]:
    edges = set()
    add_suggestions = defaultdict(set)
    unused_edges = set()
    include_changes = None

    if changes:
        # To avoid redundant filtering, allow providing an already filtered list
        if isinstance(changes, FilteredIncludeChangeList):
            include_changes = changes
        else:
            include_changes = filter_changes(
                changes,
                ignores=ignores,
                filter_generated_files=filter_generated_files,
                filter_mojom_headers=filter_mojom_headers,
                filter_third_party=filter_third_party,
                header_mappings=header_mappings,
            )

        for change_type_value, _, includer, included, *_ in include_changes:
            change_type = IncludeChange.from_value(change_type_value)

            if change_type is None:
                logging.warning(f"Skipping unknown change type: {change_type_value}")
                continue

            included = normalize_include_path(
                include_analysis, includer, included, include_directories=include_directories
            )

            if change_type is IncludeChange.REMOVE:
                unused_edges.add((includer, included))
            elif change_type is IncludeChange.ADD:
                add_suggestions[includer].add(included)

    def expand_includes(includer, included):
        # Normally we want to treat libc++ headers as opaque, unless the full option is true
        if not full and includer.startswith("third_party/libc++/src/include/"):
            return

        edge = (includer, included)

        if edge in edges:
            return

        # If we're applying changes and this edge is unused, then stop here
        if apply_changes and edge in unused_edges:
            return

        if ignore_edge and edge == ignore_edge:
            return

        edges.add((includer, included))

        if included in include_analysis["includes"]:
            for transitive_include in include_analysis["includes"][included]:
                expand_includes(included, transitive_include)

            # Inject any add suggestions here
            if changes and not remove_only:
                for added_include in add_suggestions[included]:
                    expand_includes(included, added_include)

    for include in include_analysis["includes"][filename]:
        expand_includes(filename, include)

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
        # If include changes are provided, only output unused edges, unless
        # the apply_changes option is true, in which case continue as normal
        if not apply_changes and include_changes and (includer, included) not in unused_edges:
            continue

        try:
            weight = edge_weights[includer][included] if metric else None
        except KeyError:
            if metric == "file_size":
                weight = get_include_file_size(include_analysis, included)
            else:
                weight = None

        yield (includer, included, weight)


def main():
    parser = argparse.ArgumentParser(description="List transitive (and direct) includes of a file")
    parser.add_argument(
        "include_analysis_output",
        type=argparse.FileType("r"),
        help="The include analysis output to use.",
    )
    parser.add_argument("filename", help="File to list includes for.")
    parser.add_argument("--config", help="Name of config file to use.")
    parser.add_argument(
        "--include-changes",
        type=argparse.FileType("r"),
        help="CSV of include changes to filter.",
    )
    parser.add_argument(
        "--metric",
        choices=["centrality", "expanded_size", "file_size", "includer_size", "input_size", "prevalence"],
        default="input_size",
        help="Metric to use for edge weights.",
    )
    parser.add_argument(
        "--filter-third-party", action="store_true", help="Filter out third_party/ (excluding blink) and v8."
    )
    parser.add_argument("--no-filter-generated-files", action="store_true", help="Don't filter out generated files.")
    parser.add_argument("--no-filter-mojom-headers", action="store_true", help="Don't filter out mojom headers.")
    parser.add_argument("--no-filter-ignores", action="store_true", help="Don't filter out ignores.")
    parser.add_argument(
        "--apply-changes",
        action="store_true",
        help="Apply the supplied include changes (remove unused includes, add missing ones).",
    )
    parser.add_argument(
        "--full", action="store_true", help="List all transitive includes, even those inside system headers."
    )
    parser.add_argument("--verbose", action="store_true", default=False, help="Enable verbose logging.")
    args = parser.parse_args()

    if args.apply_changes and not args.include_changes:
        print("error: --apply-changes option requires --include-changes")
        return 1

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
        for row in list_transitive_includes(
            include_analysis,
            args.filename,
            args.metric,
            changes=list(csv.reader(args.include_changes)) if args.include_changes else None,
            ignores=ignores,
            filter_generated_files=not args.no_filter_generated_files,
            filter_mojom_headers=not args.no_filter_mojom_headers,
            filter_third_party=args.filter_third_party,
            header_mappings=config.headerMappings if config else None,
            include_directories=config.includeDirs if config else None,
            apply_changes=args.apply_changes,
            full=args.full,
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
