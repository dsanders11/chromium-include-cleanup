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
from list_includers import list_includers
from list_transitive_includes import list_transitive_includes
from typing import Dict, Iterator, Set, Tuple
from utils import load_config


def add_to_remove_include(
    include_analysis: IncludeAnalysisOutput,
    changes_file: typing.TextIO,
    filename: str,
    include: str,
    minimal=False,
    ignores: IgnoresConfiguration = None,
    filter_third_party=False,
    header_mappings: Dict[str, str] = None,
) -> Iterator[Tuple[str, str]]:
    downstream_files: Set[str] = set()
    upstream_headers: Set[str] = set()

    # Include the file being modified since it may need some of the upstream headers
    downstream_files.add(filename)

    for includer, *_ in list_includers(
        include_analysis,
        filename,
        transitive=True,
        ignores=ignores,
        filter_third_party=filter_third_party,
        header_mappings=header_mappings,
    ):
        downstream_files.add(includer)

    # Include the header being removed itself since some downstream files might need it
    upstream_headers.add(include)

    # There's no need to list transitive includes of system headers
    if not include.startswith("<"):
        for _, included, *_ in list_transitive_includes(
            include_analysis,
            include,
            ignores=ignores,
            filter_third_party=filter_third_party,
            header_mappings=header_mappings,
        ):
            upstream_headers.add(included)

    add_changes = filter_changes(
        csv.reader(changes_file),
        ignores=ignores,
        change_type_filter=IncludeChange.ADD,
        filter_third_party=filter_third_party,
        header_mappings=header_mappings,
    )

    minimal_transitive_includes_cache = {}

    for _, _, includer, included, *_ in add_changes:
        if includer in downstream_files and included in upstream_headers:
            if minimal:
                # Check if it's already being pulled in transitively
                transitively_included = False

                if includer not in minimal_transitive_includes_cache:
                    minimal_transitive_includes_cache[includer] = tuple(
                        list_transitive_includes(
                            include_analysis,
                            includer,
                            ignores=ignores,
                            ignore_edge=(filename, include),
                            filter_third_party=filter_third_party,
                            header_mappings=header_mappings,
                        )
                    )

                for _, included_header, *_ in minimal_transitive_includes_cache[includer]:
                    if included_header == included:
                        transitively_included = True
                        break

            if not minimal or not transitively_included:
                yield (includer, included)


def main():
    parser = argparse.ArgumentParser(
        description="Determine which missing include edges need to be added to remove a specific include"
    )
    parser.add_argument(
        "changes_file",
        type=argparse.FileType("r"),
        help="CSV of include changes.",
    )
    parser.add_argument(
        "include_analysis_output",
        type=argparse.FileType("r"),
        help="The include analysis output to use.",
    )
    parser.add_argument("filename", help="File that has the include to be removed.")
    parser.add_argument("include", help="Filename of the include to remove")
    parser.add_argument("--config", help="Name of config file to use.")
    parser.add_argument(
        "--minimal", action="store_true", help="Only output missing includes that wouldn't be satisfied transitively."
    )
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

    if args.filename not in include_analysis["files"]:
        print(f"error: {args.filename} is not a known file")
        return 1

    if args.include not in include_analysis["files"] and not args.include.startswith("<"):
        print(f"error: {args.include} is not a known file")
        return 1

    try:
        for row in add_to_remove_include(
            include_analysis,
            args.changes_file,
            args.filename,
            args.include,
            minimal=args.minimal,
            ignores=ignores,
            filter_third_party=args.filter_third_party,
            header_mappings=config.headerMappings if config else None,
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
