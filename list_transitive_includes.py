#!/usr/bin/env python3

import argparse
import csv
import logging
import os
import sys

from common import IncludeChange
from include_analysis import ParseError, parse_raw_include_analysis_output
from utils import load_config


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
        choices=["input_size", "prevalence"],
        default="input_size",
        help="Metric to use for edge weights.",
    )
    # parser.add_argument("--no-filter-generated-files", action="store_true", help="Don't filter out generated files.")
    # parser.add_argument("--no-filter-mojom-headers", action="store_true", help="Don't filter out mojom headers.")
    # parser.add_argument("--no-filter-libc++-internal-headers", action="store_true", help="Don't filter out libc++ internal headers.")
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

    if args.config:
        config = load_config(args.config)

    if args.filename not in include_analysis["files"]:
        print(f"error: {args.filename} is not a known file")
        return 1

    unused_edges = set()

    if args.include_changes:
        try:
            for change_type_value, _, filename, header, *_ in csv.reader(args.include_changes):
                change_type = IncludeChange.from_value(change_type_value)

                if change_type is None:
                    logging.warning(f"Skipping unknown change type: {change_type_value}")
                    continue

                if change_type is IncludeChange.REMOVE:
                    unused_edges.add((filename, header))
        except csv.Error as e:
            print(f"error: Could not parse include changes file: {e}")
            return 3

    csv_writer = csv.writer(sys.stdout)
    root_count = len(include_analysis["roots"])

    edges = set()

    def expand_includes(includer, included):
        if (includer, included) in edges:
            return

        edges.add((includer, included))

        if included in include_analysis["includes"]:
            for transitive_include in include_analysis["includes"][included]:
                expand_includes(included, transitive_include)

    try:
        for include in include_analysis["includes"][args.filename]:
            if include.startswith("third_party/libc++/src/include/"):
                continue

            expand_includes(args.filename, include)

        for includer, included in edges:
            # If include changes are provided, skip edges which are not unused
            if args.include_changes and (includer, included) not in unused_edges:
                continue

            if args.metric == "prevalence":
                weight = (100.0 * include_analysis["prevalence"][includer]) / root_count
            elif args.metric == "input_size":
                weight = include_analysis["esizes"][includer][included]

            csv_writer.writerow((includer, included, weight))

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
