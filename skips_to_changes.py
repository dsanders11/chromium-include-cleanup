#!/usr/bin/env python3

import argparse
import csv
import logging
import os
import sys
from typing import Tuple

from common import IncludeChange


def main():
    parser = argparse.ArgumentParser(description="Make a full changes list from skips")

    parser.add_argument("skips")
    parser.add_argument("--verbose", action="store_true", default=False, help="Enable verbose logging.")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    with open(args.skips, "r", newline="") as f:
        skips = [row for row in csv.reader(f) if row]

    csv_writer = csv.writer(sys.stdout)

    try:
        for includer, included in skips:
            csv_writer.writerow((IncludeChange.REMOVE.value, 0, includer, included))
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
