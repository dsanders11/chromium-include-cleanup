#!/usr/bin/env python3

import argparse
import csv
import logging
import pathlib
import re
import sys
from typing import Callable, List, Tuple

from tqdm import tqdm

# Insert this script's directory into the path so it can import sibling modules
# TODO - Is this actually necessary?
sys.path.insert(0, pathlib.Path(__file__).parent.resolve())

from clangd_lsp import get_unused_includes
from include_analysis import parse_raw_include_analysis_output


def find_unused_edges(
    include_analysis: dict, progress_callback: Callable[[str], None] = None
) -> List[Tuple[str, str, int]]:
    """Finds unused edges according to clangd and returns a list of [includer, included, asize]"""

    unused_edges = []

    for root in include_analysis["roots"]:
        for unused_include in get_unused_includes(root):
            try:
                unused_edges.append(
                    (
                        root,
                        unused_include,
                        include_analysis["esizes"][root][unused_include],
                    )
                )
            except KeyError:
                logging.error(
                    f"clangd returned an unused include not in the include analysis output: {unused_include}"
                )

        if progress_callback:
            progress_callback(root)

    return unused_edges


def main():
    parser = argparse.ArgumentParser(
        description="Find unused edges, guided by the JSON analysis data from analyze_includes.py"
    )
    parser.add_argument(
        "include_analysis_output",
        type=argparse.FileType("r"),
        help="The include analysis output to use (- for stdin).",
    )
    parser.add_argument(
        "--root-filter", help="Regex to filter which root files are analyzed."
    )
    args = parser.parse_args()

    try:
        root_filter = re.compile(args.root_filter) if args.root_filter else None
    except Exception:
        print("error: --root-filter is not a valid regex")
        return 1

    include_analysis = parse_raw_include_analysis_output(
        args.include_analysis_output.read(), root_filter=root_filter
    )

    if not include_analysis:
        print("Could not process include analysis output file")
        return 1

    # This script only needs a few of the keys from include analysis
    include_analysis = {
        "roots": include_analysis["roots"],
        "esizes": include_analysis["esizes"],
    }

    progress_output = tqdm(total=len(include_analysis["roots"]))
    progress_output.display()
    unused_edges = find_unused_edges(
        include_analysis, progress_callback=lambda _: progress_output.update()
    )
    progress_output.close()

    # Dump unused edges as CSV output on stdout
    sys.stdout.reconfigure(newline="")
    csv_writer = csv.writer(sys.stdout)
    csv_writer.writerows(unused_edges)

    return 0


if __name__ == "__main__":
    sys.exit(main())
