#!/usr/bin/env python3

import argparse
import csv
import logging
import os
import sys
from typing import List, Optional, Set, Tuple

import networkx as nx

from cut_header import compute_added_sizes
from include_analysis import IncludeAnalysisOutput, ParseError, load_include_analysis
from utils import create_graph_from_include_analysis


def create_include_graph(
    include_analysis: IncludeAnalysisOutput,
    skips: Optional[Tuple[Tuple[str, str]]],
) -> nx.DiGraph:
    DG: nx.DiGraph = create_graph_from_include_analysis(include_analysis)
    files = include_analysis["files"]

    if skips:
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


def find_entry_points(
    include_analysis: IncludeAnalysisOutput,
    DG: nx.DiGraph,
    subset: Set[str],
) -> List[str]:
    """Find entry points into a subset of nodes.

    An entry point is a node in the subset that is still reachable from
    at least one root after all edges *between* nodes in the subset have
    been removed.  This identifies which subset nodes are the first to
    appear on paths from roots into the subset cluster.
    """
    files = include_analysis["files"]
    file_idx_lookup = {filename: idx for idx, filename in enumerate(files)}
    subset_indices = {file_idx_lookup[f] for f in subset if f in file_idx_lookup}

    DG2 = DG.copy()

    # Remove all edges between nodes in the subset
    edges_to_remove = []
    for u in subset_indices:
        for _, v in DG2.out_edges(u):
            if v in subset_indices:
                edges_to_remove.append((u, v))
        for v, _ in DG2.in_edges(u):
            if v in subset_indices:
                edges_to_remove.append((v, u))

    DG2.remove_edges_from(edges_to_remove)

    # For each root, find which subset nodes are still reachable
    entry_points = set()

    root_indices = set()
    for root in include_analysis["roots"]:
        if root in file_idx_lookup:
            root_indices.add(file_idx_lookup[root])

    for root_idx in root_indices:
        reachable = nx.descendants(DG2, root_idx)
        reachable_subset = reachable & subset_indices
        entry_points.update(reachable_subset)

    return sorted(files[idx] for idx in entry_points)


def find_top_edges(
    include_analysis: IncludeAnalysisOutput,
    subset: Set[str],
    top_n: int = 10,
    ignores: Optional[Set[Tuple[str, str]]] = None,
) -> List[Tuple[str, str, float]]:
    """Find the top N edges between nodes inside the subset, ranked by prevalence.

    Returns a list of (includer, included, prevalence) tuples sorted by
    prevalence descending.
    """
    root_count = len(include_analysis["roots"])
    edges = []

    for included in subset:
        for includer in include_analysis["included_by"].get(included, []):
            if includer not in subset:
                continue
            if ignores and (includer, included) in ignores:
                continue
            prevalence = (100.0 * include_analysis["prevalence"][includer]) / root_count
            edges.append((includer, included, prevalence))

    # Sort by prevalence descending, take top N
    edges.sort(key=lambda x: x[2], reverse=True)
    return edges[:top_n]


def find_top_edges_by_dominators(
    include_analysis: IncludeAnalysisOutput,
    subset: Set[str],
    dominators: dict,
    top_n: int = 10,
    ignores: Optional[Set[Tuple[str, str]]] = None,
) -> List[Tuple[str, str, float, int]]:
    """Find the top N edges between nodes inside the subset, ranked by dominator count.

    Returns a list of (includer, included, prevalence, dominator_count) tuples
    sorted by dominator count descending.
    """
    root_count = len(include_analysis["roots"])
    edges = []

    for included in subset:
        for includer in include_analysis["included_by"].get(included, []):
            if includer not in subset:
                continue
            if ignores and (includer, included) in ignores:
                continue
            prevalence = (100.0 * include_analysis["prevalence"][includer]) / root_count
            dom_count = dominators.get((includer, included), 0)
            edges.append((includer, included, prevalence, dom_count))

    # Sort by dominator count descending, take top N
    edges.sort(key=lambda x: x[3], reverse=True)
    return edges[:top_n]


# Adapted from analyze_includes.py in Chromium
def compute_doms(DG: nx.DiGraph, roots):
    # Give each node a size of 1 to represent one file
    sizes = {data["filename"]: 1 for _, data in DG.nodes(data=True) if "filename" in data}

    # Split each src -> dst edge in includes into src -> (src,dst) -> dst, so that
    # we can compute how much each include graph edge adds to the size by doing
    # dominance analysis on the (src,dst) nodes.
    augmented_includes = {}
    for src_node_id, src_data in DG.nodes(data=True):
        if "filename" not in src_data:
            continue

        src = src_data["filename"]
        if src not in augmented_includes:
            augmented_includes[src] = set()

        for dst_node_id in DG.successors(src_node_id):
            dst = DG.nodes(data=True)[dst_node_id]["filename"]
            augmented_includes[src].add((src, dst))
            augmented_includes[(src, dst)] = {dst}

    return compute_added_sizes((roots, augmented_includes, sizes))


def main():
    parser = argparse.ArgumentParser(
        description="Find entry points and top edges for high-prevalence headers in the include graph."
    )
    parser.add_argument(
        "include_analysis_output",
        type=str,
        nargs="?",
        help="The include analysis output to use (can be a file path or URL). If not specified, pulls the latest.",
    )
    parser.add_argument("--skips", action="append", default=[], help="CSV files of edges to skip (remove from graph).")
    parser.add_argument("--ignores", action="append", default=[], help="CSV files of edges to ignore.")
    parser.add_argument(
        "--min-prevalence",
        type=float,
        required=True,
        help="Minimum prevalence percentage for a node to be in the subset.",
    )
    parser.add_argument(
        "--top", type=int, default=10, help="Number of top edges to output by prevalence (default: 10)."
    )
    parser.add_argument("--verbose", action="store_true", default=False, help="Enable verbose logging.")
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(message)s",
        level=logging.DEBUG if args.verbose else logging.WARNING,
    )

    try:
        include_analysis = load_include_analysis(args.include_analysis_output)
    except ParseError as e:
        message = str(e)
        print("error: Could not parse include analysis output file")
        if message:
            print(message)
        return 2

    root_count = len(include_analysis["roots"])

    # Load skips and ignores from CSV files
    skips: Set[Tuple[str, str]] = set()
    ignores: Set[Tuple[str, str]] = set()

    for skips_file in args.skips:
        with open(skips_file, "r", newline="") as f:
            skips.update(
                [tuple(row) for row in csv.reader(f) if row and row[0].strip() and not row[0].startswith("#")]
            )

    for ignores_file in args.ignores:
        with open(ignores_file, "r", newline="") as f:
            ignores.update([tuple(row) for row in csv.reader(f) if row])

    # Build the subset: filter out generated/system/third-party headers and keep those meeting minimum prevalence
    EXCLUDED_PREFIXES = ("out/", "buildtools/", "build/", "third_party/", "v8/")
    EXCLUDED_EXCEPTIONS = ("third_party/blink/",)
    subset: Set[str] = set()

    for filename in include_analysis["files"]:
        if filename.startswith(EXCLUDED_PREFIXES) and not filename.startswith(EXCLUDED_EXCEPTIONS):
            continue

        prevalence = (100.0 * include_analysis["prevalence"].get(filename, 0)) / root_count

        if prevalence >= args.min_prevalence:
            subset.add(filename)

    logging.info(f"Subset size: {len(subset)} nodes with >= {args.min_prevalence:.2f}% prevalence")

    if not subset:
        print(f"No nodes meet the minimum prevalence of {args.min_prevalence:.2f}%", file=sys.stderr)
        return 0

    DG: nx.DiGraph = create_include_graph(include_analysis, skips)

    entry_points = find_entry_points(include_analysis, DG, subset)
    dominators = compute_doms(
        DG.subgraph([include_analysis["files"].index(node) for node in subset]).copy(), entry_points
    )

    # Find and output top N edges by prevalence
    top_edges = find_top_edges(include_analysis, subset, top_n=args.top, ignores=ignores)

    # Find top N edges by dominator count
    top_edges_by_doms = find_top_edges_by_dominators(
        include_analysis, subset, dominators, top_n=args.top, ignores=ignores
    )

    print(f"Top {args.top} edges by prevalence:", file=sys.stderr)

    try:
        csv_writer = csv.writer(sys.stdout)
        for includer, included, prevalence in top_edges:
            csv_writer.writerow([includer, included, f"{prevalence:.2f}", dominators.get((includer, included), 0)])

        print(f"\nTop {args.top} edges by dominator count:", file=sys.stderr)

        for includer, included, prevalence, dom_count in top_edges_by_doms:
            csv_writer.writerow([includer, included, f"{prevalence:.2f}", dom_count])

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
