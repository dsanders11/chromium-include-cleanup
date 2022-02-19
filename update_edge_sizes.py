#!/usr/bin/env python3

import argparse
import csv
import logging
import pathlib
import sys
import typing
from typing import Dict, Iterator, Optional, Tuple

# Insert this script's directory into the path so it can import sibling modules
# TODO - Is this actually necessary?
sys.path.insert(0, pathlib.Path(__file__).parent.resolve())

from common import IncludeChange
from include_analysis import ParseError, parse_raw_include_analysis_output
from utils import get_edge_sizes


def update_edge_sizes(
    changes_file: typing.TextIO, edge_sizes: Dict[str, Dict[str, int]]
) -> Iterator[Tuple[IncludeChange, int, str, str, Optional[int]]]:
    """Update edge sizes in the include changes output"""

    change_type_value: str

    for change_type_value, *change in csv.reader(changes_file):
        change_type = IncludeChange.from_value(change_type_value)

        # Only removes have edge sizes
        if change_type is IncludeChange.REMOVE:
            line, filename, header = change[:3]

            if len(change) == 4:
                edge_size = change[3]
            else:
                edge_size = None

            if filename not in edge_sizes:
                logging.debug(f"Skipping filename not in include analysis output, file may be removed: {filename}")
                continue

            if header not in edge_sizes[filename]:
                # If it's None in the original output, keep it, otherwise skip it
                if edge_size is not None:
                    logging.debug(f"Skipping edge not in include analysis output: {filename},{header}")
                    continue
            else:
                # Update the edge size
                change = (line, filename, header, edge_sizes[filename][header])

        full_change: Tuple[IncludeChange, int, str, str, Optional[int]] = (change_type_value, *change)
        yield full_change


def main():
    parser = argparse.ArgumentParser(description="Update edge sizes in include changes output")
    parser.add_argument(
        "changes_file",
        type=argparse.FileType("r"),
        help="CSV of include changes to update.",
    )
    parser.add_argument(
        "include_analysis_output",
        type=argparse.FileType("r"),
        help="The include analysis output to use.",
    )
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

    edge_sizes = get_edge_sizes(include_analysis)
    csv_writer = csv.writer(sys.stdout)

    for row in update_edge_sizes(args.changes_file, edge_sizes):
        csv_writer.writerow(row)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass  # Don't show the user anything
