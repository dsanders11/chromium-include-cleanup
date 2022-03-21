#!/usr/bin/env python3

import argparse
import csv
import logging
import os
import pathlib
import re
import sys
from typing import List, Tuple

from pydantic import BaseModel

# Insert this script's directory into the path so it can import sibling modules
# TODO - Is this actually necessary?
sys.path.insert(0, pathlib.Path(__file__).parent.resolve())

from common import IncludeChange

Change = Tuple[IncludeChange, int, str, str, int]


class IgnoresSubConfiguration(BaseModel):
    filenames: List[str] = []
    headers: List[str] = []
    edges: List[Tuple[str, str]] = []


class IgnoresConfiguration(BaseModel):
    add: IgnoresSubConfiguration = IgnoresSubConfiguration()
    remove: IgnoresSubConfiguration = IgnoresSubConfiguration()


GENERATED_FILE_REGEX = re.compile(r"^out/\w+/gen/.*$")
MOJOM_HEADER_REGEX = re.compile(r"^.*.mojom[^.]*.h$")


def filter_changes(
    changes: List[Change],
    ignores: IgnoresConfiguration = None,
    filename_filter: re.Pattern = None,
    header_filter: re.Pattern = None,
    change_type_filter: IncludeChange = None,
    filter_generated_files=True,
    filter_mojom_headers=True,
):
    """Filter changes"""

    for change_type_value, line, filename, header, *_ in changes:
        change_type = IncludeChange.from_value(change_type_value)

        if change_type is None:
            logging.warning(f"Skipping unknown change type: {change_type_value}")
            continue
        elif change_type_filter and change_type != change_type_filter:
            continue

        if filename_filter and not filename_filter.match(filename):
            continue
        elif header_filter and not header_filter.match(header):
            continue

        if filter_generated_files and GENERATED_FILE_REGEX.match(filename):
            continue

        if filter_mojom_headers and MOJOM_HEADER_REGEX.match(header):
            continue

        # Cut down on noise by using ignores
        if ignores:
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

        yield (change_type_value, line, filename, header, *_)


def main():
    parser = argparse.ArgumentParser(description="Filter include changes output")
    parser.add_argument(
        "changes_file",
        type=argparse.FileType("r"),
        help="CSV of include changes to filter.",
    )
    parser.add_argument("--filename-filter", help="Regex to filter which files have changes outputted.")
    parser.add_argument("--header-filter", help="Regex to filter which headers are included in the changes.")
    parser.add_argument("--ignores", default="chromium", help="Name of ignores file to use.")
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

    if args.no_filter_ignores:
        ignores = None
    else:
        ignores_config_file = pathlib.Path(__file__).parent.joinpath("ignores", args.ignores).with_suffix(".json")

        if not ignores_config_file.exists():
            print(f"error: no ignores config file found: {ignores_config_file}")
            return 1

        ignores = IgnoresConfiguration.parse_file(ignores_config_file)

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
