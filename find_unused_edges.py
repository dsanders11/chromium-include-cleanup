#!/usr/bin/env python3

import argparse
import asyncio
import csv
import logging
import pathlib
import re
import sys
from typing import Callable, Dict, List, Tuple

from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

# Insert this script's directory into the path so it can import sibling modules
# TODO - Is this actually necessary?
sys.path.insert(0, pathlib.Path(__file__).parent.resolve())

from clangd_lsp import ClangdClient
from include_analysis import parse_raw_include_analysis_output


async def find_unused_edges(
    clangd_client: ClangdClient,
    filenames: List[str],
    edge_sizes: Dict[str, Dict[str, int]],
    progress_callback: Callable[[str], None] = None,
) -> List[Tuple[str, str, int]]:
    """Finds unused edges according to clangd and returns a list of [includer, included, asize]"""

    unused_edges = []

    for filename in filenames:
        try:
            for unused_include in await clangd_client.get_unused_includes(filename):
                try:
                    unused_edges.append(
                        (
                            filename,
                            unused_include,
                            edge_sizes[filename][unused_include],
                        )
                    )
                except KeyError:
                    logging.error(
                        f"clangd returned an unused include not in the include analysis output: {unused_include}"
                    )
        except Exception:
            logging.exception(f"Skipping file due to unexpected error: {filename}")

        if progress_callback:
            progress_callback(filename)

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
    parser.add_argument("--filename-filter", help="Regex to filter which files are analyzed.")
    parser.add_argument("--verbose", action="store_true", default=False, help="Enable verbose logging.")
    args = parser.parse_args()

    try:
        filename_filter = re.compile(args.filename_filter) if args.filename_filter else None
    except Exception:
        print("error: --filename-filter is not a valid regex")
        return 1

    if args.compile_commands_dir and not args.compile_commands_dir.is_dir():
        print("error: --compile-commands-dir must be a directory")
        return 1

    include_analysis = parse_raw_include_analysis_output(args.include_analysis_output.read(), strip_gen_prefix=True)

    if not include_analysis:
        print("error: Could not process include analysis output file")
        return 2

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    # Filter out some files we know we don't want to process, like the system headers
    filenames = [
        filename for filename in include_analysis["files"] if not re.match(r"^(?:buildtools|build)/", filename)
    ]

    # Further filter the filenames if a filter was provided, so not all files are processed
    filenames = [
        filename
        for filename in filenames
        if not filename_filter or (filename_filter and filename_filter.match(filename))
    ]

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
        progress_output = tqdm(total=len(filenames), unit="file")
        await clangd_client.start()

        with logging_redirect_tqdm():
            progress_output.display()
            unused_edges = await find_unused_edges(
                clangd_client,
                filenames,
                include_analysis["esizes"],
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
