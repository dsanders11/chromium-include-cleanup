#!/usr/bin/env python3

import argparse
import concurrent.futures
import csv
import logging
import sys
from itertools import batched
from typing import Set, Tuple

import networkx as nx
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

from cut_header import compute_direct_cuts, compute_doms_to_target, calculate_floors
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
    parser.add_argument("--ignores", action="append", default=[], help="Edges to ignore when determining cuts.")
    parser.add_argument("--skips", action="append", default=[], help="Edges to skip when determining cuts.")
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
                    if all_cuts_floor_pct < args.max_floor and include_analysis["tsizes"].get(header, 0) >= args.min_tsize:
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

    print(f"\n{len(results)} headers with all_cuts_floor_pct < {args.max_floor}%", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
