#!/usr/bin/env python3

import argparse
import asyncio
import csv
import logging
import pathlib
import re
import sys
from typing import Callable, List, Tuple

from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

# Insert this script's directory into the path so it can import sibling modules
# TODO - Is this actually necessary?
sys.path.insert(0, pathlib.Path(__file__).parent.resolve())

from clangd_lsp import ClangdClient
from include_analysis import parse_raw_include_analysis_output


async def find_unused_edges(
    clangd_client: ClangdClient,
    include_analysis: dict,
    progress_callback: Callable[[str], None] = None,
) -> List[Tuple[str, str, int]]:
    """Finds unused edges according to clangd and returns a list of [includer, included, asize]"""

    unused_edges = []

    for root in include_analysis["roots"]:
        for unused_include in await clangd_client.get_unused_includes(root):
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


# TODO - Ctrl+C doesn't cleanly exit
# TODO - How to detect when compilation DB isn't found and clangd is falling back (won't work)
async def main():
    parser = argparse.ArgumentParser(
        description="Find unused edges, guided by the JSON analysis data from analyze_includes.py"
    )
    parser.add_argument(
        "include_analysis_output",
        type=argparse.FileType("r"),
        help="The include analysis output to use (- for stdin).",
    )
    parser.add_argument(
        "--clangd-path",
        type=pathlib.Path,
        help="Path to the clangd executable to use.",
        default="clangd.exe" if sys.platform == "win32" else "clangd",
    )
    parser.add_argument(
        "--chromium-src", type=pathlib.Path, help="Path to the Chromium source tree.", default=pathlib.Path(".")
    )
    parser.add_argument(
        "--compile-commands-dir", type=pathlib.Path, help="Specify a path to look for compile_commands.json."
    )
    parser.add_argument("--root-filter", help="Regex to filter which root files are analyzed.")
    parser.add_argument("--verbose", action="store_true", default=False, help="Enable verbose logging.")
    args = parser.parse_args()

    try:
        root_filter = re.compile(args.root_filter) if args.root_filter else None
    except Exception:
        print("error: --root-filter is not a valid regex")
        return 1

    if args.compile_commands_dir and not args.compile_commands_dir.is_dir():
        print("error: --compile-commands-dir must be a directory")
        return 1

    include_analysis = parse_raw_include_analysis_output(args.include_analysis_output.read(), root_filter=root_filter)

    if not include_analysis:
        print("error: Could not process include analysis output file")
        return 2

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    # This script only needs a few of the keys from include analysis
    include_analysis = {
        "roots": include_analysis["roots"],
        "esizes": include_analysis["esizes"],
    }

    root_path = args.chromium_src.resolve()

    if not ClangdClient.validate_config(root_path):
        print("error: Must have a .clangd config with IncludeCleaner enabled")
        return 3

    clangd_client = ClangdClient(
        args.clangd_path,
        root_path,
        args.compile_commands_dir.resolve() if args.compile_commands_dir else None,
    )

    unused_edges = None

    try:
        progress_output = tqdm(total=len(include_analysis["roots"]))
        await clangd_client.start()

        with logging_redirect_tqdm():
            progress_output.display()
            unused_edges = await find_unused_edges(
                clangd_client,
                include_analysis,
                progress_callback=lambda _: progress_output.update(),
            )
    finally:
        progress_output.close()
        await clangd_client.exit()

    if unused_edges:
        # Dump unused edges as CSV output on stdout
        sys.stdout.reconfigure(newline="")
        csv_writer = csv.writer(sys.stdout)
        csv_writer.writerows(unused_edges)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
