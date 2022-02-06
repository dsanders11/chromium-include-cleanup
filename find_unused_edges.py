#!/usr/bin/env python3

import argparse
import asyncio
import csv
import logging
import pathlib
import re
import sys
from typing import AsyncIterator, Callable, Dict, Tuple

from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

# Insert this script's directory into the path so it can import sibling modules
# TODO - Is this actually necessary?
sys.path.insert(0, pathlib.Path(__file__).parent.resolve())

from clangd_lsp import ClangdClient, ClangdCrashed
from include_analysis import parse_raw_include_analysis_output
from utils import get_worker_count


async def find_unused_edges(
    clangd_client: ClangdClient,
    work_queue: asyncio.Queue,
    edge_sizes: Dict[str, Dict[str, int]],
    progress_callback: Callable[[str], None] = None,
) -> AsyncIterator[Tuple[str, str, int]]:
    """Finds unused edges according to clangd and yields them as (includer, included, asize)"""

    unused_edges = asyncio.Queue()

    async def worker():
        while work_queue.qsize() > 0:
            filename = work_queue.get_nowait()

            try:
                for unused_include in await clangd_client.get_unused_includes(filename):
                    try:
                        unused_edges.put_nowait(
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
            except ClangdCrashed:
                logging.error(f"Skipping file due to clangd crash: {filename}")
                raise
            except FileNotFoundError:
                logging.error(f"Skipping file due to file not found: {filename}")
            except Exception:
                logging.exception(f"Skipping file due to unexpected error: {filename}")
            finally:
                if progress_callback:
                    progress_callback(filename)

    worker_count = get_worker_count()

    work = asyncio.gather(*[worker() for _ in range(worker_count)])

    try:
        while not work.done():
            unused_edge_task = asyncio.create_task(unused_edges.get())
            done, _ = await asyncio.wait({unused_edge_task, work}, return_when=asyncio.FIRST_COMPLETED)
            if unused_edge_task in done:
                yield unused_edge_task.result()
            else:
                unused_edge_task.cancel()
                break
    except asyncio.CancelledError:
        pass

    await work


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

    include_analysis = parse_raw_include_analysis_output(args.include_analysis_output.read())

    if not include_analysis:
        print("error: Could not process include analysis output file")
        return 2

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    # Filter out some files we know we don't want to process, like the system headers, and *.inc files
    filenames = [
        filename
        for filename in include_analysis["files"]
        if not re.match(r"^(?:buildtools|build)/", filename) and not filename.endswith(".inc")
    ]

    # Further filter the filenames if a filter was provided, so not all files are processed
    filenames = [
        filename
        for filename in filenames
        if not filename_filter or (filename_filter and filename_filter.match(filename))
    ]

    work_queue = asyncio.Queue()

    # Fill the queue with the filenames to process
    for filename in filenames:
        work_queue.put_nowait(filename)

    # Strip off the path prefix for generated file includes so matching will work
    generated_file_prefix = re.compile(r"^(?:out/\w+/gen/)?(.*)$")

    edge_sizes = {
        filename: {
            generated_file_prefix.match(included).group(1): size
            for included, size in include_analysis["esizes"][filename].items()
        }
        for filename in include_analysis["esizes"]
    }

    root_path = args.chromium_src.resolve()

    if not ClangdClient.validate_config(root_path):
        print("error: Must have a .clangd config with IncludeCleaner enabled")
        return 3

    async def start_clangd_client():
        clangd_client = ClangdClient(
            args.clangd_path,
            root_path,
            args.compile_commands_dir.resolve() if args.compile_commands_dir else None,
        )
        await clangd_client.start()

        return clangd_client

    csv_writer = csv.writer(sys.stdout)

    with logging_redirect_tqdm(), tqdm(total=work_queue.qsize(), unit="file") as progress_output:
        clangd_client = await start_clangd_client()

        try:
            while work_queue.qsize() > 0:
                try:
                    # Incrementally output the unused edges so that it doesn't need to
                    # wait for hours before any output happens, when something could crash
                    async for unused_edge in find_unused_edges(
                        clangd_client,
                        work_queue,
                        edge_sizes,
                        progress_callback=lambda _: progress_output.update(),
                    ):
                        csv_writer.writerow(unused_edge)
                except ClangdCrashed:
                    # Make sure the old client is cleaned up
                    await clangd_client.exit()

                    # Start a new one and continue
                    clangd_client = await start_clangd_client()
        finally:
            await clangd_client.exit()

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass  # Don't show the user anything
