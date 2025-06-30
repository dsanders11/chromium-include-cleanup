#!/usr/bin/env python3

import argparse
import csv
import logging
import os
import sys
import typing
from typing import Dict, Iterator, List, Optional, Tuple

from common import IgnoresConfiguration, IncludeChange
from filter_include_changes import filter_changes
from include_analysis import IncludeAnalysisOutput, ParseError, parse_raw_include_analysis_output
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


def set_edge_weights(
    include_analysis: IncludeAnalysisOutput,
    changes_file: typing.TextIO,
    edge_weights: Dict[str, Dict[str, int]],
    filter_third_party: bool = False,
    ignores: IgnoresConfiguration = None,
    header_mappings: Dict[str, str] = None,
    include_directories: List[str] = None,
) -> Iterator[Tuple[IncludeChange, int, str, str, Optional[int]]]:
    """Set edge weights in the include changes output"""

    change_type_value: str

    filtered_changes = filter_changes(
        csv.reader(changes_file),
        filter_third_party=filter_third_party,
        ignores=ignores,
        header_mappings=header_mappings,
    )

    for change_type_value, line, filename, include, *_ in filtered_changes:
        change_type = IncludeChange.from_value(change_type_value)
        change = (line, filename, include)

        # Normalize from clangd's short form includes to the long form in the include
        # analysis output (e.g. <vector> -> third_party/libc++/src/include/vector)
        include = normalize_include_path(include_analysis, filename, include, include_directories=include_directories)

        if change_type is IncludeChange.REMOVE:
            # For now, only removes have edge weights
            if filename not in edge_weights:
                logging.warning(f"Skipping filename not found in weights, file may have been removed: {filename}")
            elif include not in edge_weights[filename]:
                logging.warning(f"Skipping edge not found in weights: {filename},{include}")
            else:
                change = change + (edge_weights[filename][include],)
        elif change_type is IncludeChange.ADD:
            # TODO - Some metric for how important they are to add, if there
            #        is one? Maybe something like the ratio of occurrences to
            #        direct includes, suggesting it's used a lot, but has lots
            #        of missing includes? That metric wouldn't really work well
            #        since leaf headers of commonly included headers would end
            #        up with a high ratio, despite not really being important to
            #        add anywhere. Maybe there's no metric here and instead an
            #        analysis is done at the end to rank headers by how many
            #        suggested includes there are for that file.
            pass

        full_change: Tuple[IncludeChange, int, str, str, Optional[int]] = (change_type_value, *change)
        yield full_change


def main():
    parser = argparse.ArgumentParser(description="Set edge weights in include changes output")
    parser.add_argument(
        "changes_file",
        type=argparse.FileType("r"),
        help="CSV of include changes to set edge weights for.",
    )
    parser.add_argument(
        "include_analysis_output",
        type=argparse.FileType("r"),
        help="The include analysis output to use.",
    )
    parser.add_argument(
        "--metric",
        choices=["centrality", "expanded_size", "file_size", "includer_size", "input_size", "prevalence"],
        default="input_size",
        help="Metric to use for edge weights.",
    )
    parser.add_argument("--config", help="Name of config file to use.")
    parser.add_argument(
        "--filter-third-party", action="store_true", help="Filter out third_party/ (excluding blink) and v8."
    )
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

    if args.metric == "input_size":
        edge_weights = get_include_analysis_edge_sizes(include_analysis)
    elif args.metric == "expanded_size":
        edge_weights = get_include_analysis_edge_expanded_sizes(include_analysis)
    elif args.metric == "file_size":
        edge_weights = get_include_analysis_edge_file_sizes(include_analysis)
    elif args.metric == "centrality":
        edge_weights = get_include_analysis_edges_centrality(include_analysis)
    elif args.metric == "prevalence":
        edge_weights = get_include_analysis_edge_prevalence(include_analysis)
    elif args.metric == "includer_size":
        edge_weights = get_include_analysis_edge_includer_size(include_analysis)

    try:
        for row in set_edge_weights(
            include_analysis,
            args.changes_file,
            edge_weights,
            filter_third_party=args.filter_third_party,
            ignores=ignores,
            header_mappings=config.headerMappings if config else None,
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
