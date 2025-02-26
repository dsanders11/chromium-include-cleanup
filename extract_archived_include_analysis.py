#!/usr/bin/env python3

import argparse
import logging
import os
import re
import sys
import urllib.request

DATA_REGEX = re.compile(r".*<script>\n?(data = .*?)<\/script>", re.DOTALL)


def main():
    parser = argparse.ArgumentParser(description="Extract archived include analysis JSON")
    parser.add_argument("include_analysis_url", help="The include analysis output URL to extract.")
    parser.add_argument("--verbose", action="store_true", default=False, help="Enable verbose logging.")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    contents = urllib.request.urlopen(args.include_analysis_url).read()

    data_match = DATA_REGEX.match(contents.decode("utf-8"))

    try:
        if data_match:
            print(data_match.group(1).strip())

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
