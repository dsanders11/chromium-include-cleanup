#!/usr/bin/env python3

import argparse
import csv
import logging
import os
import sys

from common import FilteredIncludeChangeList, IgnoresConfiguration, IncludeChange
from filter_include_changes import Change, filter_changes
from list_transitive_includes import list_transitive_includes
from include_analysis import IncludeAnalysisOutput, ParseError, parse_raw_include_analysis_output
from typing import Callable, Dict, Iterator, List, Tuple
from utils import load_config


def recalculate_expanded_sizes(
    include_analysis: IncludeAnalysisOutput,
    filenames: List[str],
    changes: List[Change],
    progress_callback: Callable[[str], None] = None,
    ignores: IgnoresConfiguration = None,
    filter_generated_files=True,
    filter_mojom_headers=True,
    filter_third_party=False,
    header_mappings: Dict[str, str] = None,
    include_directories: List[str] = None,
    remove_only=False,
) -> Iterator[Tuple[str, int]]:
    include_changes = FilteredIncludeChangeList(
        filter_changes(
            changes,
            ignores=ignores,
            filter_generated_files=filter_generated_files,
            filter_mojom_headers=filter_mojom_headers,
            filter_third_party=filter_third_party,
            header_mappings=header_mappings,
        )
    )

    for filename in filenames:
        # Recalculate all of the transitive includes for this file
        includes = list_transitive_includes(
            include_analysis,
            filename,
            metric="file_size",
            changes=include_changes,
            ignores=ignores,
            filter_generated_files=filter_generated_files,
            filter_mojom_headers=filter_mojom_headers,
            filter_third_party=filter_third_party,
            header_mappings=header_mappings,
            include_directories=include_directories,
            apply_changes=True,
            full=True,
            remove_only=remove_only,
        )

        # The expanded size for the file is all of its include sizes, and its own size
        expanded_size = sum(map(lambda entry: entry[1], set(map(lambda entry: entry[1:3], includes))))
        expanded_size += include_analysis["sizes"][filename]

        if expanded_size > include_analysis["tsizes"][filename]:
            logging.warning(
                f"{filename} unexpectedly increased in size from {include_analysis["tsizes"][filename]} to {expanded_size}"
            )

        yield (filename, expanded_size)

        if progress_callback:
            progress_callback(filename)


def main():
    from tqdm import tqdm
    from tqdm.contrib.logging import logging_redirect_tqdm

    parser = argparse.ArgumentParser(
        description="Recalculate translation unit expanded sizes if all provided include changes were applied"
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
    parser.add_argument(
        "filenames", nargs="*", help="File(s) to recalculate for - if not specified, does all translation units."
    )
    parser.add_argument("--config", help="Name of config file to use.")
    parser.add_argument(
        "--filter-third-party", action="store_true", help="Filter out third_party/ (excluding blink) and v8."
    )
    parser.add_argument("--no-filter-generated-files", action="store_true", help="Don't filter out generated files.")
    parser.add_argument("--no-filter-mojom-headers", action="store_true", help="Don't filter out mojom headers.")
    parser.add_argument("--no-filter-ignores", action="store_true", help="Don't filter out ignores.")
    parser.add_argument("--remove-only", action="store_true", help="Only apply remove include suggestions.")
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

    if not args.filenames:
        filenames = include_analysis["roots"]
    else:
        filenames = args.filenames

        for filename in filenames:
            if filename not in include_analysis["roots"]:
                print(f"error: {filename} is not a known root file")
                return 1

    try:
        with logging_redirect_tqdm(), tqdm(
            disable=len(filenames) == 1, total=len(filenames), unit="file"
        ) as progress_output:
            for row in recalculate_expanded_sizes(
                include_analysis,
                filenames,
                list(csv.reader(args.changes_file)),
                progress_callback=lambda _: progress_output.update(),
                ignores=ignores,
                filter_generated_files=not args.no_filter_generated_files,
                filter_mojom_headers=not args.no_filter_mojom_headers,
                filter_third_party=args.filter_third_party,
                header_mappings=config.headerMappings if config else None,
                include_directories=config.includeDirs if config else None,
                remove_only=args.remove_only
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
