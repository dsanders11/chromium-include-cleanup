#!/usr/bin/env python3

import argparse
import csv
import logging
import os
import pathlib
import sys
import typing
from typing import Dict, Iterator, Optional, Tuple

from common import IncludeChange
from include_analysis import ParseError, parse_raw_include_analysis_output
from utils import get_edge_sizes, load_config


def set_edge_weights(
    changes_file: typing.TextIO, edge_weights: Dict[str, Dict[str, int]]
) -> Iterator[Tuple[IncludeChange, int, str, str, Optional[int]]]:
    """Set edge weights in the include changes output"""

    change_type_value: str

    for change_type_value, line, filename, header, *_ in csv.reader(changes_file):
        change_type = IncludeChange.from_value(change_type_value)
        change = (line, filename, header)

        if change_type is IncludeChange.REMOVE:
            # For now, only removes have edge weights
            if filename not in edge_weights:
                logging.warning(f"Skipping filename not found in weights, file may be removed: {filename}")
            elif header not in edge_weights[filename]:
                logging.warning(f"Skipping edge not found in weights: {filename},{header}")
            else:
                change = change + (edge_weights[filename][header],)
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


# TODO - More metrics for determining the weight of an edge
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
    parser.add_argument("--config", help="Name of config file to use.")
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

    edge_sizes = get_edge_sizes(include_analysis, config.includeDirs if config else None)
    csv_writer = csv.writer(sys.stdout)

    try:
        for row in set_edge_weights(args.changes_file, edge_sizes):
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
