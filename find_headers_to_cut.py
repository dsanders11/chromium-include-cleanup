#!/usr/bin/env python3

import argparse
import concurrent.futures
import csv
import curses
import logging
import os
import sys
from itertools import batched
from typing import Set, Tuple

import networkx as nx
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

from cut_header import (
    compute_direct_cuts,
    compute_doms_to_target,
    copy_to_clipboard,
    calculate_floors,
    is_gist_url,
    load_edges_from_file,
    run_interactive as cut_header_run_interactive,
)
from include_analysis import IncludeAnalysisOutput, ParseError, load_include_analysis
from utils import create_graph_from_include_analysis

_worker_include_analysis = None
_worker_ignores = None
_worker_skips = None


def _init_worker(include_analysis_output, ignores, skips):
    global _worker_include_analysis, _worker_ignores, _worker_skips
    _worker_include_analysis = load_include_analysis(include_analysis_output)
    _worker_ignores = ignores
    _worker_skips = skips


def calculate_results(headers):
    results = []

    for header in headers:
        floors = calculate_floors(_worker_include_analysis, header, ignores=_worker_ignores, skips=_worker_skips)

        edge_dominations = compute_doms_to_target(_worker_include_analysis, floors["DG"], header)
        top_directs = compute_direct_cuts(_worker_include_analysis, floors["DG"], header, edge_dominations)
        top_directs.sort(key=lambda x: x[3], reverse=True)

        results.append(
            (header, floors["remaining_pct"], floors["all_cuts_floor_pct"], top_directs[0][3] if top_directs else 0)
        )

    return results


def create_modified_include_graph(
    include_analysis: IncludeAnalysisOutput,
    skips: Tuple[Tuple[str, str]],
) -> nx.DiGraph:
    DG: nx.DiGraph = create_graph_from_include_analysis(include_analysis)
    files = include_analysis["files"]

    for includer, included in skips:
        if includer in files and included in files:
            includer_idx = files.index(includer)
            included_idx = files.index(included)

            if DG.has_edge(includer_idx, included_idx):
                DG.remove_edge(includer_idx, included_idx)
            else:
                logging.warning(f"Skip edge {includer} -> {included} not found in include graph")
        else:
            logging.warning(f"Skip edge {includer} -> {included} not found in include analysis")

    return DG


def run_interactive(
    pre_calculated_output: str,
    top_n: int,
    include_analysis=None,
    ignores_files=None,
    skips_files=None,
    gh_token=None,
):
    """Run the interactive curses-based TUI for browsing pre-calculated header results."""

    with open(pre_calculated_output, "r", newline="") as f:
        reader = csv.reader(f)
        rows = [row for row in reader if row and row[0].strip()]

    # Sort by top_direct_dominated (column 3) descending
    rows.sort(key=lambda r: int(r[3]) if len(r) > 3 else 0, reverse=True)
    rows = rows[:top_n]

    if not rows:
        print("No rows found in pre-calculated output.", file=sys.stderr)
        return

    # Track headers that have been acted on in cut_header's interactive mode
    acted_on_headers: set = set()

    def render(stdscr, rows, selected_idx, scroll_offset):
        """Render the TUI. Returns nothing."""
        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()

        row = 0

        def addstr(y, x, text, attr=0):
            if y < max_y:
                try:
                    stdscr.addnstr(y, x, text, max_x - x - 1, attr)
                except curses.error:
                    pass

        # Title
        addstr(row, 0, f"Headers to cut ({len(rows)} results)", curses.A_BOLD)
        row += 1

        # Column headers
        addstr(row, 3, "header", curses.A_UNDERLINE)
        # Right-align column headers at fixed positions
        col_remaining = max_x - 45
        col_floor = max_x - 33
        col_dominated = max_x - 20
        col_tsize = max_x - 10
        if col_remaining > 10:
            addstr(row, col_remaining, "remain%", curses.A_UNDERLINE)
            addstr(row, col_floor, "floor%", curses.A_UNDERLINE)
            addstr(row, col_dominated, "dominated", curses.A_UNDERLINE)
            addstr(row, col_tsize, "tsize", curses.A_UNDERLINE)
        row += 1

        # Available lines for data rows (reserve 2 for footer)
        available_lines = max_y - row - 2

        visible_rows = rows[scroll_offset : scroll_offset + available_lines]

        for i, data_row in enumerate(visible_rows):
            actual_idx = scroll_offset + i
            is_selected = actual_idx == selected_idx

            header = data_row[0] if len(data_row) > 0 else ""
            remaining_pct = data_row[1] if len(data_row) > 1 else ""
            all_cuts_floor_pct = data_row[2] if len(data_row) > 2 else ""
            top_direct_dominated = data_row[3] if len(data_row) > 3 else ""
            tsize = data_row[4] if len(data_row) > 4 else ""

            is_acted_on = header in acted_on_headers
            base_attr = curses.A_BOLD if is_selected else 0
            if is_acted_on:
                base_attr |= curses.color_pair(1)  # Green for acted-on headers

            if is_selected:
                addstr(row, 0, "*", curses.color_pair(1) | curses.A_BOLD)

            addstr(row, 2, header, base_attr)

            if col_remaining > 10:
                if is_acted_on:
                    # Values are stale after acting on header, show "?"
                    addstr(row, col_remaining, "?", curses.color_pair(4) | base_attr)
                    addstr(row, col_floor, "?", curses.color_pair(4) | base_attr)
                    addstr(row, col_dominated, "?", curses.color_pair(5) | base_attr)
                else:
                    addstr(row, col_remaining, remaining_pct, curses.color_pair(4) | base_attr)
                    addstr(row, col_floor, all_cuts_floor_pct, curses.color_pair(4) | base_attr)
                    addstr(row, col_dominated, top_direct_dominated, curses.color_pair(5) | base_attr)
                tsize_attr = curses.A_BOLD if is_selected else 0
                addstr(row, col_tsize, tsize, tsize_attr)

            row += 1

        # Scroll indicator
        footer_row = max_y - 2
        if len(rows) > available_lines:
            addstr(footer_row, 0, f"[{selected_idx + 1}/{len(rows)}]", curses.A_DIM)

        # Footer help
        can_enter = include_analysis is not None and ignores_files and skips_files
        has_stale = len(acted_on_headers) > 0
        if can_enter:
            parts = ["[↑/↓] Select", "[Enter] Inspect"]
            if has_stale:
                parts.append("[r] Refresh")
            parts.extend(["[c] Copy header", "[q] Quit"])
            addstr(max_y - 1, 0, "  ".join(parts), curses.A_DIM)
        else:
            addstr(max_y - 1, 0, "[↑/↓] Select  [c] Copy header  [q] Quit", curses.A_DIM)

        stdscr.refresh()

    def curses_main(stdscr):
        curses.curs_set(0)  # Hide cursor
        stdscr.keypad(True)

        # Init colors
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)  # Selected asterisk / acted-on headers
        curses.init_pair(2, curses.COLOR_CYAN, -1)  # Unused, reserved
        curses.init_pair(3, curses.COLOR_RED, -1)  # Unused, reserved
        curses.init_pair(4, curses.COLOR_YELLOW, -1)  # Percentages
        curses.init_pair(5, curses.COLOR_MAGENTA, -1)  # Dominated edges

        selected_idx = 0
        scroll_offset = 0

        while True:
            max_y, _ = stdscr.getmaxyx()
            available_lines = max_y - 4  # title + column header + 2 footer lines

            # Ensure scroll keeps selected item visible
            if selected_idx < scroll_offset:
                scroll_offset = selected_idx
            elif selected_idx >= scroll_offset + available_lines:
                scroll_offset = selected_idx - available_lines + 1

            render(stdscr, rows, selected_idx, scroll_offset)

            key = stdscr.getch()

            if key == ord("q"):
                break
            elif key == ord("c"):
                if 0 <= selected_idx < len(rows):
                    header = rows[selected_idx][0] if rows[selected_idx] else ""
                    copy_to_clipboard(header)
            elif key == ord("r"):
                if include_analysis is not None and ignores_files and skips_files and acted_on_headers:
                    # Show computing message
                    stdscr.erase()
                    stdscr.addstr(0, 0, "Refreshing modified headers... please wait", curses.A_BOLD)
                    stdscr.refresh()

                    # Load current ignores and skips from files
                    ignores: Set[Tuple[str, str]] = set()
                    for f in ignores_files:
                        ignores.update(load_edges_from_file(f))
                    skips: Set[Tuple[str, str]] = set()
                    for f in skips_files:
                        skips.update(load_edges_from_file(f))

                    # Recalculate for each acted-on header
                    for row_data in rows:
                        header = row_data[0] if row_data else ""
                        if header in acted_on_headers:
                            try:
                                floors = calculate_floors(
                                    include_analysis,
                                    header,
                                    ignores=tuple(ignores),
                                    skips=tuple(skips),
                                )
                                edge_dominations = compute_doms_to_target(
                                    include_analysis,
                                    floors["DG"],
                                    header,
                                )
                                top_directs = compute_direct_cuts(
                                    include_analysis,
                                    floors["DG"],
                                    header,
                                    edge_dominations,
                                )
                                top_directs.sort(key=lambda x: x[3], reverse=True)
                                top_direct_dominated = top_directs[0][3] if top_directs else 0

                                row_data[1] = f"{floors['remaining_pct']:.2f}"
                                row_data[2] = f"{floors['all_cuts_floor_pct']:.2f}"
                                row_data[3] = str(top_direct_dominated)
                            except Exception:
                                pass  # Keep stale data on error

                    acted_on_headers.clear()
                    stdscr.redrawwin()
                    stdscr.refresh()
            elif key in (curses.KEY_ENTER, 10, 13):
                if include_analysis is not None and ignores_files and skips_files and 0 <= selected_idx < len(rows):
                    header = rows[selected_idx][0] if rows[selected_idx] else ""
                    if header and header in include_analysis["files"]:
                        # Exit curses temporarily to run cut_header's interactive mode
                        curses.endwin()
                        modified = cut_header_run_interactive(
                            include_analysis=include_analysis,
                            target=header,
                            ignores_files=ignores_files,
                            skips_files=skips_files,
                            top_n=15,
                            sort_by="dominated",
                            gh_token=gh_token,
                            nested=True,
                        )
                        if modified:
                            acted_on_headers.add(header)
                        # Re-init curses
                        stdscr = curses.initscr()
                        curses.noecho()
                        curses.cbreak()
                        stdscr.keypad(True)
                        curses.curs_set(0)
                        curses.start_color()
                        curses.use_default_colors()
                        curses.init_pair(1, curses.COLOR_GREEN, -1)
                        curses.init_pair(2, curses.COLOR_CYAN, -1)
                        curses.init_pair(3, curses.COLOR_RED, -1)
                        curses.init_pair(4, curses.COLOR_YELLOW, -1)
                        curses.init_pair(5, curses.COLOR_MAGENTA, -1)
                        stdscr.redrawwin()
                        stdscr.refresh()
            elif key == curses.KEY_UP:
                selected_idx = (selected_idx - 1) % len(rows)
            elif key == curses.KEY_DOWN:
                selected_idx = (selected_idx + 1) % len(rows)
            elif key == curses.KEY_PPAGE:  # Page Up
                selected_idx = max(0, selected_idx - available_lines)
            elif key == curses.KEY_NPAGE:  # Page Down
                selected_idx = min(len(rows) - 1, selected_idx + available_lines)
            elif key == curses.KEY_HOME:
                selected_idx = 0
            elif key == curses.KEY_END:
                selected_idx = len(rows) - 1

    curses.wrapper(curses_main)


def main():
    parser = argparse.ArgumentParser(description="Find headers that are candidates for cutting.")
    parser.add_argument(
        "include_analysis_output",
        type=str,
        nargs="?",
        help="The include analysis output to use (can be a file path or URL). If not specified, pulls the latest.",
    )
    parser.add_argument(
        "--min-prevalence",
        type=float,
        default=2.0,
        help="Minimum prevalence percentage of total roots to consider (default: 2.0).",
    )
    parser.add_argument(
        "--max-prevalence",
        type=float,
        default=20.0,
        help="Maximum prevalence percentage of total roots to consider (default: 20.0).",
    )
    parser.add_argument(
        "--max-floor",
        type=float,
        default=75.0,
        help="Maximum all_cuts_floor_pct to include in output (default: 75.0).",
    )
    parser.add_argument(
        "--min-tsize",
        type=int,
        default=0,
        help="Minimum tsize (translated size) to include in output (default: 0).",
    )
    ignores_group = parser.add_mutually_exclusive_group()
    ignores_group.add_argument("--ignores", action="append", default=[], help="Edges to ignore when determining cuts.")
    ignores_group.add_argument("--ignores-file", type=str, help="File containing edges to ignore (one per line).")

    skips_group = parser.add_mutually_exclusive_group()
    skips_group.add_argument("--skips", action="append", default=[], help="Edges to skip when determining cuts.")
    skips_group.add_argument("--skips-file", type=str, help="File containing edges to skip (one per line).")
    parser.add_argument("--interactive", action="store_true", default=False, help="Run in interactive mode.")
    parser.add_argument(
        "--pre-calculated-output",
        type=str,
        help="A pre-calculated output file to use from a previous run (requires --interactive).",
    )
    parser.add_argument(
        "--top", type=int, default=5, help="Number of top headers to display (default: 5, requires --interactive)."
    )
    parser.add_argument("--verbose", action="store_true", default=False, help="Enable verbose logging.")
    args = parser.parse_args()

    if args.pre_calculated_output and not args.interactive:
        parser.error("--pre-calculated-output requires --interactive")
    if args.top != 5 and not args.interactive:
        parser.error("--top requires --interactive")
    if args.interactive and not args.pre_calculated_output:
        parser.error("--interactive requires --pre-calculated-output")

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    if args.ignores_file:
        with open(args.ignores_file) as f:
            args.ignores = [line.strip() for line in f if line.strip()]

    if args.skips_file:
        with open(args.skips_file) as f:
            args.skips = [line.strip() for line in f if line.strip()]

    if args.interactive:
        if len(args.ignores) == 0:
            parser.error("error: interactive mode requires at least one ignores file")

        if len(args.skips) == 0:
            parser.error("error: interactive mode requires at least one skips file")

        gh_token = os.environ.get("GH_TOKEN")

        if is_gist_url(args.ignores[0]) and not gh_token:
            parser.error("error: the first ignores is a gist URL but GH_TOKEN environment variable is not set")

        if is_gist_url(args.skips[0]) and not gh_token:
            parser.error("error: the first skips is a gist URL but GH_TOKEN environment variable is not set")

    try:
        include_analysis = load_include_analysis(args.include_analysis_output)
    except ParseError as e:
        message = str(e)
        print("error: Could not parse include analysis output file")
        if message:
            print(message)
        return 2

    if args.interactive:
        ignores_files = args.ignores if args.ignores else None
        skips_files = args.skips if args.skips else None

        run_interactive(
            args.pre_calculated_output,
            args.top,
            include_analysis=include_analysis,
            ignores_files=ignores_files,
            skips_files=skips_files,
            gh_token=gh_token,
        )
        return 0

    ignores: Set[Tuple[str, str]] = set()
    skips: Set[Tuple[str, str]] = set()

    for ignores_file in args.ignores:
        with open(ignores_file, "r", newline="") as f:
            ignores.update(
                [tuple(row) for row in csv.reader(f) if row and row[0].strip() and not row[0].startswith("#")]
            )

    for skips_file in args.skips:
        with open(skips_file, "r", newline="") as f:
            skips.update(
                [tuple(row) for row in csv.reader(f) if row and row[0].strip() and not row[0].startswith("#")]
            )

    for edge in ignores.intersection(skips):
        logging.warning(
            f"warning: edge {edge[0]} -> {edge[1]} is in both ignores and skips, it will be treated as skipped"
        )

    modified_include_graph = create_modified_include_graph(include_analysis, tuple(skips))

    total_roots = len(include_analysis["roots"])
    prevalence = include_analysis["prevalence"]
    min_count = args.min_prevalence / 100.0 * total_roots
    max_count = args.max_prevalence / 100.0 * total_roots

    EXCLUDED_PREFIXES = ("out/", "buildtools/", "build/", "third_party/", "v8/")
    EXCLUDED_EXCEPTIONS = ("third_party/blink/",)

    # Find headers with prevalence above the threshold
    candidates = [
        header
        for header, count in prevalence.items()
        if count > min_count
        and count <= max_count
        and header.endswith(".h")
        and not (header.startswith(EXCLUDED_PREFIXES) and not header.startswith(EXCLUDED_EXCEPTIONS))
    ]

    # Recalculate prevalence for candidates using modified include graph (with skip edges removed)
    files = include_analysis["files"]
    file_idx_lookup = {filename: idx for idx, filename in enumerate(files)}
    root_indices = {file_idx_lookup[root] for root in include_analysis["roots"] if root in file_idx_lookup}

    original_candidate_count = len(candidates)
    recalculated_candidates = []

    for header in candidates:
        if header in file_idx_lookup:
            header_idx = file_idx_lookup[header]
            if header_idx in modified_include_graph:
                ancestors = nx.ancestors(modified_include_graph, header_idx)
                modified_count = len(ancestors & root_indices)
                if modified_count > min_count:
                    recalculated_candidates.append(header)

    candidates = recalculated_candidates

    print(
        f"Found {len(candidates)} headers with prevalence {args.min_prevalence}% - {args.max_prevalence}% of {total_roots} roots"
    )
    print(
        f"{original_candidate_count - len(candidates)} candidates removed after recalculating prevalence with skip edges removed"
    )

    chunk_size = 4
    chunked = list(batched(candidates, chunk_size))
    ignores_tuple = tuple(ignores)
    skips_tuple = tuple(skips)

    results = []
    with logging_redirect_tqdm(), tqdm(
        disable=len(candidates) <= 1, total=len(candidates), unit="header"
    ) as progress_output:
        with concurrent.futures.ProcessPoolExecutor(
            initializer=_init_worker, initargs=(args.include_analysis_output, ignores_tuple, skips_tuple)
        ) as pool:
            for chunk_results in pool.map(calculate_results, chunked):
                progress_output.update(min(chunk_size, progress_output.total - progress_output.n))

                for header, remaining_pct, all_cuts_floor_pct, top_direct_dominated in chunk_results:
                    if (
                        all_cuts_floor_pct < args.max_floor
                        and include_analysis["tsizes"].get(header, 0) >= args.min_tsize
                    ):
                        results.append((header, remaining_pct, all_cuts_floor_pct, top_direct_dominated))

    # Sort by top_direct_dominated
    results.sort(key=lambda r: r[3])

    writer = csv.writer(sys.stdout)
    for header, remaining_pct, all_cuts_floor_pct, top_direct_dominated in results:
        writer.writerow(
            [
                header,
                f"{remaining_pct:.2f}",
                f"{all_cuts_floor_pct:.2f}",
                top_direct_dominated,
                include_analysis["tsizes"].get(header, 0),
            ]
        )

    print(
        f"\n{len(results)} headers with all_cuts_floor_pct < {args.max_floor}% and tsize >= {args.min_tsize}",
        file=sys.stderr,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
