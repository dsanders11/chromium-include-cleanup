#!/usr/bin/env python3

import argparse
import asyncio
import csv
import enum
import logging
import pathlib
import re
import sys
from typing import AsyncIterator, Callable, Dict, Optional, Tuple

from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

# Insert this script's directory into the path so it can import sibling modules
# TODO - Is this actually necessary?
sys.path.insert(0, pathlib.Path(__file__).parent.resolve())

from clangd_lsp import ClangdClient, ClangdCrashed
from include_analysis import ParseError, parse_raw_include_analysis_output
from utils import get_worker_count


class IncludeChange(enum.Enum):
    ADD = "add"
    REMOVE = "remove"


async def suggest_include_changes(
    clangd_client: ClangdClient,
    work_queue: asyncio.Queue,
    edge_sizes: Dict[str, Dict[str, int]] = None,
    progress_callback: Callable[[str], None] = None,
) -> AsyncIterator[Tuple[IncludeChange, str, str, Optional[int]]]:
    """Suggest includes to add or remove according to clangd and yield them as (change, includer, included, [size])"""

    suggested_changes: asyncio.Queue[Tuple[IncludeChange, str, str, Optional[int]]] = asyncio.Queue()

    async def worker():
        while work_queue.qsize() > 0:
            filename = work_queue.get_nowait()

            try:
                add, remove = await clangd_client.get_include_suggestions(filename)

                for include in add:
                    # TODO - Some metric for how important they are to add, if there
                    #        is one? Maybe something like the ratio of occurrences to
                    #        direct includes, suggesting it's used a lot, but has lots
                    #        of missing includes? That metric wouldn't really work well
                    #        since leaf headers of commonly included headers would end
                    #        up with a high ratio, despite not really being important to
                    #        add anywhere. Maybe there's no metric here and instead an
                    #        analysis is done at the end to rank headers by how many
                    #        suggested includes there are for that file.
                    suggested_changes.put_nowait(
                        (
                            IncludeChange.ADD,
                            filename,
                            include,
                        )
                    )

                for include in remove:
                    edge_size = None

                    if edge_sizes:
                        try:
                            edge_size = edge_sizes[filename][include]
                        except KeyError:
                            logging.error(
                                f"clangd returned an unused include not in the include analysis output: {include}"
                            )

                    change = (
                        IncludeChange.REMOVE,
                        filename,
                        include,
                    )

                    suggested_changes.put_nowait(change if edge_size is None else (*change, edge_size))
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
        while not work.done() or suggested_changes.qsize() > 0:
            suggested_include_task = asyncio.create_task(suggested_changes.get())
            done, _ = await asyncio.wait({suggested_include_task, work}, return_when=asyncio.FIRST_COMPLETED)
            if suggested_include_task in done:
                yield suggested_include_task.result()
            else:
                suggested_include_task.cancel()
                break
    except asyncio.CancelledError:
        pass

    await work


# TODO - How to detect when compilation DB isn't found and clangd is falling back (won't work)
async def main():
    parser = argparse.ArgumentParser(
        description="Suggest includes to add or remove, guided by the JSON analysis data from analyze_includes.py"
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

    if args.compile_commands_dir and not args.compile_commands_dir.is_dir():
        print("error: --compile-commands-dir must be a directory")
        return 1

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

    # Filter out some files we know we don't want to process, like the system headers, and non-source files
    filenames = [
        filename
        for filename in include_analysis["files"]
        if not re.match(r"^(?:buildtools|build)/", filename)
        and not filename.endswith(".sigs")
        and not filename.endswith(".def")
        and not filename.endswith(".inc")
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
                    async for include_change in suggest_include_changes(
                        clangd_client,
                        work_queue,
                        edge_sizes,
                        progress_callback=lambda _: progress_output.update(),
                    ):
                        if args.add_only and include_change[0] is IncludeChange.ADD:
                            csv_writer.writerow(include_change[1:])
                        elif args.remove_only and include_change[0] is IncludeChange.REMOVE:
                            csv_writer.writerow(include_change[1:])
                        elif not args.add_only and not args.remove_only:
                            csv_writer.writerow((include_change[0].value, *include_change[1:]))
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
