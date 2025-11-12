#!/usr/bin/env python3

import argparse
import csv
import logging
import math
import multiprocessing
import os
import queue
import sys
import time
from itertools import batched

from common import FilteredIncludeChangeList, IgnoresConfiguration
from filter_include_changes import Change, filter_changes
from list_transitive_includes import list_transitive_includes
from include_analysis import IncludeAnalysisOutput, ParseError, load_include_analysis
from typing import Callable, Dict, Iterator, List, Tuple
from utils import get_worker_count, load_config


def recalculate_expanded_size(
    include_analysis: IncludeAnalysisOutput,
    filename: str,
    changes: List[Change],
    ignores: IgnoresConfiguration = None,
    filter_generated_files=True,
    filter_mojom_headers=True,
    filter_third_party=False,
    header_mappings: Dict[str, str] = None,
    include_directories: List[str] = None,
    remove_only=False,
) -> Tuple[str, int]:
    # Recalculate all of the transitive includes for this file
    includes = list_transitive_includes(
        include_analysis,
        filename,
        metric="file_size",
        changes=changes,
        ignores=ignores,
        filter_generated_files=filter_generated_files,
        filter_mojom_headers=filter_mojom_headers,
        filter_third_party=filter_third_party,
        header_mappings=header_mappings,
        include_directories=include_directories,
        apply_changes=True,
        full=True,
        remove_only=remove_only,
    )

    # The expanded size for the file is all of its include sizes, and its own size
    expanded_size = sum(map(lambda entry: entry[1], set(map(lambda entry: entry[1:3], includes))))
    expanded_size += include_analysis["sizes"][filename]

    if expanded_size > include_analysis["tsizes"][filename]:
        logging.warning(
            f"{filename} unexpectedly increased in size from {include_analysis["tsizes"][filename]} to {expanded_size} - ignoring"
        )
        expanded_size = include_analysis["tsizes"][filename]

    return (filename, expanded_size)


def work_func(
    filenames: List[str],
    result_queue: multiprocessing.JoinableQueue,
    include_analysis: IncludeAnalysisOutput,
    changes: List[Change],
    ignores: IgnoresConfiguration = None,
    filter_generated_files=True,
    filter_mojom_headers=True,
    filter_third_party=False,
    header_mappings: Dict[str, str] = None,
    include_directories: List[str] = None,
    remove_only=False,
):
    try:
        for filename in filenames:
            result = recalculate_expanded_size(
                include_analysis,
                filename,
                changes,
                ignores=ignores,
                filter_generated_files=filter_generated_files,
                filter_mojom_headers=filter_mojom_headers,
                filter_third_party=filter_third_party,
                header_mappings=header_mappings,
                include_directories=include_directories,
                remove_only=remove_only,
            )

            result_queue.put_nowait(result)

        # Don't exit until the result queue has been fully consumed
        result_queue.join()
    except KeyboardInterrupt:
        pass  # Don't show the user anything


def recalculate_expanded_sizes(
    filenames: List[str],
    include_analysis: IncludeAnalysisOutput,
    changes: List[Change],
    progress_callback: Callable[[str], None] = None,
    ignores: IgnoresConfiguration = None,
    filter_generated_files=False,
    filter_mojom_headers=False,
    filter_third_party=False,
    header_mappings: Dict[str, str] = None,
    include_directories: List[str] = None,
    remove_only=False,
) -> Iterator[Tuple[str, int]]:
    result_queue: multiprocessing.JoinableQueue[Tuple[str, int]] = multiprocessing.JoinableQueue()

    include_changes = FilteredIncludeChangeList(
        filter_changes(
            changes,
            ignores=ignores,
            filter_generated_files=filter_generated_files,
            filter_mojom_headers=filter_mojom_headers,
            filter_third_party=filter_third_party,
            header_mappings=header_mappings,
        )
    )

    worker_count = min(len(filenames), get_worker_count())
    chunk_size = math.ceil(float(len(filenames)) / worker_count)

    chunked = list(batched(filenames, chunk_size))

    workers = [
        multiprocessing.Process(
            target=work_func,
            args=(chunked[idx], result_queue, include_analysis, include_changes),
            kwargs={
                "ignores": ignores,
                "filter_generated_files": filter_generated_files,
                "filter_mojom_headers": filter_mojom_headers,
                "filter_third_party": filter_third_party,
                "header_mappings": header_mappings,
                "include_directories": include_directories,
                "remove_only": remove_only,
            },
        )
        for idx in range(worker_count)
    ]

    for worker in workers:
        worker.start()

    done = False

    while not done:
        try:
            (filename, expanded_size) = result_queue.get_nowait()
            yield (filename, expanded_size)
            progress_callback(filename)
            result_queue.task_done()
        except queue.Empty:
            pass

        # If all workers have exited, then there should be no more results
        if any((worker.is_alive() for worker in workers)):
            time.sleep(0.01)
        else:
            done = True


def main():
    from tqdm import tqdm
    from tqdm.contrib.logging import logging_redirect_tqdm

    parser = argparse.ArgumentParser(
        description="Recalculate translation unit expanded sizes if all provided include changes were applied"
    )
    parser.add_argument(
        "changes_file",
        type=argparse.FileType("r"),
        help="CSV of include changes.",
    )
    parser.add_argument(
        "include_analysis_output",
        type=str,
        help="The include analysis output to use.",
    )
    parser.add_argument(
        "filenames", nargs="*", help="File(s) to recalculate for - if not specified, does all translation units."
    )
    parser.add_argument("--config", help="Name of config file to use.")
    parser.add_argument(
        "--filter-third-party", action="store_true", help="Filter out third_party/ (excluding blink) and v8."
    )
    parser.add_argument("--no-filter-generated-files", action="store_true", help="Don't filter out generated files.")
    parser.add_argument("--no-filter-mojom-headers", action="store_true", help="Don't filter out mojom headers.")
    parser.add_argument("--no-filter-ignores", action="store_true", help="Don't filter out ignores.")
    parser.add_argument("--remove-only", action="store_true", help="Only apply remove include suggestions.")
    parser.add_argument("--verbose", action="store_true", default=False, help="Enable verbose logging.")
    args = parser.parse_args()

    try:
        include_analysis = load_include_analysis(args.include_analysis_output)
    except ParseError as e:
        message = str(e)
        print("error: Could not parse include analysis output file")
        if message:
            print(message)
        return 2

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    config = None
    ignores = None

    if args.config:
        config = load_config(args.config)

    if config and not args.no_filter_ignores:
        ignores = config.ignores

    csv_writer = csv.writer(sys.stdout)

    if not args.filenames:
        filenames = include_analysis["roots"]
    else:
        filenames = args.filenames

        for filename in filenames:
            if filename not in include_analysis["roots"]:
                print(f"error: {filename} is not a known root file")
                return 1

    try:
        with logging_redirect_tqdm(), tqdm(
            disable=len(filenames) == 1, total=len(filenames), unit="file"
        ) as progress_output:
            for row in recalculate_expanded_sizes(
                filenames,
                include_analysis,
                list(csv.reader(args.changes_file)),
                progress_callback=lambda _: progress_output.update(),
                ignores=ignores,
                filter_generated_files=not args.no_filter_generated_files,
                filter_mojom_headers=not args.no_filter_mojom_headers,
                filter_third_party=args.filter_third_party,
                header_mappings=config.headerMappings if config else None,
                include_directories=config.includeDirs if config else None,
                remove_only=args.remove_only,
            ):
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
