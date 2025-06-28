#!/usr/bin/env python3

import argparse
import asyncio
import csv
import logging
import pathlib
import re
import sys
from typing import AsyncIterator, Callable, List, Optional, Tuple

from clangd_lsp import ClangdClient, ClangdCrashed
from common import IncludeChange
from filter_include_changes import GENERATED_FILE_REGEX, MOJOM_HEADER_REGEX, THIRD_PARTY_REGEX
from include_analysis import ParseError, parse_raw_include_analysis_output
from utils import get_worker_count


def filter_filenames(
    filenames: List[str],
    filename_filter: re.Pattern = None,
    filter_generated_files=False,
    filter_mojom_headers=False,
    filter_third_party=False,
) -> List[str]:
    # Filter out some files we know we don't want to process, like the system headers, and non-source files
    # Further filter the filenames if a filter was provided, so not all files are processed
    return [
        filename
        for filename in filenames
        if not re.match(r"^(?:buildtools|build|third_party/llvm-build)/", filename)
        and not filename.endswith(".sigs")
        and not filename.endswith(".def")
        and not filename.endswith(".gen")
        and not filename.endswith(".inc")
        and not filename.endswith(".inl")
        and not filename.endswith(".s")
        and not filename.endswith(".sksl")
        and not filename.endswith(".S")
        and not filename.startswith("third_party/libc++/")
        and "/usr/include/c++/" not in filename
        and (not filename_filter or (filename_filter and filename_filter.match(filename)))
        and (not filter_generated_files or not GENERATED_FILE_REGEX.match(filename))
        and (not filter_mojom_headers or not MOJOM_HEADER_REGEX.match(filename))
        and (not filter_third_party or not THIRD_PARTY_REGEX.match(filename))
    ]


async def suggest_include_changes(
    clangd_client: ClangdClient,
    work_queue: asyncio.Queue,
    progress_callback: Callable[[str], None] = None,
    timeout=None,
) -> AsyncIterator[Tuple[IncludeChange, int, str, str, Optional[int]]]:
    """
    Suggest includes to add or remove according to clangd and yield them

    Yielded as (change, line_no, includer, included)
    """

    suggested_changes: asyncio.Queue[Tuple[IncludeChange, str, str, Optional[int]]] = asyncio.Queue()

    async def worker():
        while work_queue.qsize() > 0:
            filename = work_queue.get_nowait()

            try:
                async with asyncio.timeout(timeout):
                    add, remove = await clangd_client.get_include_suggestions(filename)

                for changes, op in ((add, IncludeChange.ADD), (remove, IncludeChange.REMOVE)):
                    for include, line in changes:
                        suggested_changes.put_nowait(
                            (
                                op,
                                line,
                                filename,
                                include,
                            )
                        )
            except ClangdCrashed:
                logging.error(f"Skipping file due to clangd crash: {filename}")
                raise
            except FileNotFoundError:
                logging.error(f"Skipping file due to file not found: {filename}")
            except TimeoutError:
                logging.error(f"Skipping file due to timeout: {filename}")
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
    from tqdm import tqdm
    from tqdm.contrib.logging import logging_redirect_tqdm

    parser = argparse.ArgumentParser(
        description="Suggest includes to add or remove, guided by the JSON analysis data from analyze_includes.py"
    )
    parser.add_argument(
        "include_analysis_output",
        type=argparse.FileType("r"),
        help="The include analysis output to use (- for stdin).",
    )
    parser.add_argument(
        "filenames", nargs="*", help="File(s) to suggest includes for - if not specified, all files are checked."
    )
    parser.add_argument(
        "--clangd-path",
        type=pathlib.Path,
        help="Path to the clangd executable to use.",
        default="clangd.exe" if sys.platform == "win32" else "clangd",
    )
    parser.add_argument(
        "--src-root", type=pathlib.Path, help="Path to the source tree root.", default=pathlib.Path(".")
    )
    parser.add_argument(
        "--compile-commands-dir", type=pathlib.Path, help="Specify a path to look for compile_commands.json."
    )
    parser.add_argument("--filename-filter", help="Regex to filter which files are analyzed.")
    parser.add_argument(
        "--restart-clangd-after", type=int, default=350, help="Restart clangd every N files processed."
    )
    parser.add_argument(
        "--timeout", type=int, default=30, help="How long to wait for suggestions on any given file."
    )
    parser.add_argument(
        "--filter-third-party", action="store_true", help="Filter out third_party/ (excluding blink) and v8."
    )
    parser.add_argument("--filter-generated-files", action="store_true", help="Filter out generated files.")
    parser.add_argument("--filter-mojom-headers", action="store_true", help="Filter out mojom headers.")
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

    root_path = args.src_root.resolve()

    if not ClangdClient.validate_config(root_path):
        print("error: Must have a .clangd config with IncludeCleaner enabled")
        return 3

    if not args.filenames:
        filenames = filter_filenames(
            include_analysis["files"],
            filename_filter,
            filter_generated_files=args.filter_generated_files,
            filter_mojom_headers=args.filter_mojom_headers,
            filter_third_party=args.filter_third_party,
        )
    else:
        filenames = args.filenames

    async def start_clangd_client():
        clangd_client = ClangdClient(
            args.clangd_path,
            root_path,
            args.compile_commands_dir.resolve() if args.compile_commands_dir else None,
        )
        await clangd_client.start()

        return clangd_client

    csv_writer = csv.writer(sys.stdout)

    with logging_redirect_tqdm(), tqdm(
        disable=len(filenames) == 1, total=len(filenames), unit="file"
    ) as progress_output:
        work_queue = asyncio.Queue()
        clangd_client: ClangdClient = None

        # Process the files in chunks, restarting clangd in between. Performance seems to
        # degrade with clangd over time as more files are processed. It's possibly a bug
        # in this script, or just that clangd is building something up every file processed
        while len(filenames) > 0 or work_queue.qsize() > 0:
            # Fill the queue with the filenames to process
            for _ in range(min(len(filenames), args.restart_clangd_after - work_queue.qsize())):
                work_queue.put_nowait(filenames.pop(0))

            try:
                clangd_client = await start_clangd_client()

                async for change_type, *include_change in suggest_include_changes(
                    clangd_client,
                    work_queue,
                    progress_callback=lambda _: progress_output.update(),
                    timeout=args.timeout,
                ):
                    csv_writer.writerow((change_type.value, *include_change))
            except ClangdCrashed:
                pass  # No special handling needed, a new clangd will be started
            finally:
                # Make sure the old client is cleaned up
                if clangd_client:
                    await clangd_client.exit()

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass  # Don't show the user anything
