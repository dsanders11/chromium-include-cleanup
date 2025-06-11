#!/usr/bin/env python3

import argparse
import csv
import logging
import os
import re
import sys
from collections import defaultdict
from typing import DefaultDict, Dict, List, Optional, Tuple

from common import IgnoresConfiguration, IncludeChange
from utils import load_config

Change = Tuple[IncludeChange, int, str, str, Optional[str]]


GENERATED_FILE_REGEX = re.compile(r"^out/[\w-]+/gen/.*$")
MOJOM_HEADER_REGEX = re.compile(r"^.*.mojom[^.]*.h$")
THIRD_PARTY_REGEX = re.compile(r"^(?:third_party\/(?!blink)|v8).*$")


def filter_changes(
    changes: List[Change],
    ignores: IgnoresConfiguration = None,
    filename_filter: re.Pattern = None,
    header_filter: re.Pattern = None,
    change_type_filter: IncludeChange = None,
    filter_generated_files=True,
    filter_mojom_headers=True,
    filter_third_party=False,
    header_mappings: Dict[str, str] = None,
    weight_threshold: float = None,
):
    """Filter changes"""

    # When header mappings are provided, we can cancel out suggestions from clangd where
    # it suggests removing one include and adding another, when the pair is found in the
    # mapping, since we know that means clangd is confused on which header to include
    pending_changes: DefaultDict[str, Dict[str, Tuple[IncludeChange, int, Optional[str]]]] = defaultdict(dict)

    for change_type_value, line, filename, header, *_ in changes:
        # Weight value may or may not be present
        if weight_threshold:
            if len(_) == 0 or (len(_) == 1 and float(_[0]) < weight_threshold):
                continue

        change_type = IncludeChange.from_value(change_type_value)

        if change_type is None:
            logging.warning(f"Skipping unknown change type: {change_type_value}")
            continue
        elif change_type_filter and change_type != change_type_filter:
            continue

        # Filter out internal system headers
        if header.startswith("<__"):
            continue

        if filename_filter and not filename_filter.match(filename):
            continue
        elif header_filter and not header_filter.match(header):
            continue

        if filter_generated_files and GENERATED_FILE_REGEX.match(filename):
            continue

        if filter_third_party and THIRD_PARTY_REGEX.match(filename):
            continue

        if filter_mojom_headers and MOJOM_HEADER_REGEX.match(header):
            continue

        # Cut down on noise by using ignores
        if ignores:
            # Some files have to be skipped because clangd infers a bad compilation command for them
            if filename in ignores.skip:
                continue

            if change_type is IncludeChange.REMOVE:
                if filename in ignores.remove.filenames:
                    logging.info(f"Skipping filename for unused includes: {filename}")
                    continue

                ignore_edge = (filename, header) in ignores.remove.edges
                ignore_include = header in ignores.remove.headers

                # TODO - Ignore unused suggestion if the include is for the associated header

                if ignore_edge or ignore_include:
                    continue
            elif change_type is IncludeChange.ADD:
                if filename in ignores.add.filenames:
                    logging.info(f"Skipping filename for adding includes: {filename}")
                    continue

                ignore_edge = (filename, header) in ignores.add.edges
                ignore_include = header in ignores.add.headers

                if ignore_edge or ignore_include:
                    continue

        # If the header is in a provided header mapping, wait until the end to yield it
        if header_mappings and header in header_mappings:
            assert header not in pending_changes[filename]

            # TODO - Includes inside of dependencies shouldn't be mapped since they can
            #        access internal headers, and the mapped canonical header is from
            #        the perspective of the project's root source directory
            pending_changes[filename][header] = (change_type, line, *_)
            continue

        yield (change_type_value, line, filename, header, *_)

    if header_mappings:
        inverse_header_mappings = {v: k for k, v in header_mappings.items()}

        for filename in pending_changes:
            for header in pending_changes[filename]:
                change_type, line, *_ = pending_changes[filename][header]

                if change_type is IncludeChange.ADD:
                    # Look for a corresponding remove which would cancel out
                    if header_mappings[header] in pending_changes[filename]:
                        if pending_changes[filename][header][0] is IncludeChange.REMOVE:
                            continue
                elif change_type is IncludeChange.REMOVE and header in inverse_header_mappings:
                    # Look for a corresponding add which would cancel out
                    if inverse_header_mappings[header] in pending_changes[filename]:
                        if pending_changes[filename][header][0] is IncludeChange.ADD:
                            continue

                yield (change_type.value, line, filename, header_mappings[header], *_)


def main():
    parser = argparse.ArgumentParser(description="Filter include changes output")
    parser.add_argument(
        "changes_file",
        type=argparse.FileType("r"),
        help="CSV of include changes to filter.",
    )
    parser.add_argument("--filename-filter", help="Regex to filter which files have changes outputted.")
    parser.add_argument("--header-filter", help="Regex to filter which headers are included in the changes.")
    parser.add_argument("--config", help="Name of config file to use.")
    parser.add_argument(
        "--weight-threshold", type=float, help="Filter out changes with a weight value below the threshold."
    )
    parser.add_argument("--filter-third-party", action="store_true", help="Filter out third_party/ (excluding blink) and v8.")
    parser.add_argument("--no-filter-generated-files", action="store_true", help="Don't filter out generated files.")
    parser.add_argument("--no-filter-mojom-headers", action="store_true", help="Don't filter out mojom headers.")
    parser.add_argument("--no-filter-ignores", action="store_true", help="Don't filter out ignores.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--add-only", action="store_true", default=False, help="Only output includes to add.")
    group.add_argument("--remove-only", action="store_true", default=False, help="Only output includes to remove.")
    parser.add_argument("--verbose", action="store_true", default=False, help="Enable verbose logging.")
    args = parser.parse_args()

    try:
        filename_filter = re.compile(args.filename_filter) if args.filename_filter else None
    except Exception:
        print("error: --filename-filter is not a valid regex")
        return 1

    try:
        header_filter = re.compile(args.header_filter) if args.header_filter else None
    except Exception:
        print("error: --header-filter is not a valid regex")
        return 1

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    if args.add_only:
        change_type_filter = IncludeChange.ADD
    elif args.remove_only:
        change_type_filter = IncludeChange.REMOVE
    else:
        change_type_filter = None

    config = None
    ignores = None

    if args.config:
        config = load_config(args.config)

    if config and not args.no_filter_ignores:
        ignores = config.ignores

    csv_writer = csv.writer(sys.stdout)

    try:
        for change in filter_changes(
            csv.reader(args.changes_file),
            ignores=ignores,
            filename_filter=filename_filter,
            header_filter=header_filter,
            change_type_filter=change_type_filter,
            filter_generated_files=not args.no_filter_generated_files,
            filter_mojom_headers=not args.no_filter_mojom_headers,
            filter_third_party=args.filter_third_party,
            header_mappings=config.headerMappings if config else None,
            weight_threshold=args.weight_threshold,
        ):
            csv_writer.writerow(change)

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
