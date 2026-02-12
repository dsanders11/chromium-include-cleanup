#!/usr/bin/env python3

import argparse
import csv
import logging
import sys
from typing import List, Optional, Set, Tuple

import networkx as nx
from collections import defaultdict
from itertools import batched
from networkx.algorithms.connectivity import minimum_st_edge_cut
from networkx.algorithms.flow import build_residual_network

from count_reachable_roots import count_reachable_roots
from include_analysis import IncludeAnalysisOutput, ParseError, load_include_analysis
from utils import create_graph_from_include_analysis


def create_include_graph(
    include_analysis: IncludeAnalysisOutput,
    target: str,
    ignores: Optional[Tuple[Tuple[str, str]]],
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

    if ignores:
        # Set capacity high for ignored edges to prevent cutting them
        for includer, included in ignores:
            if includer in files and included in files:
                includer_idx = files.index(includer)
                included_idx = files.index(included)

                if DG.has_edge(includer_idx, included_idx):
                    DG[includer_idx][included_idx]["capacity"] = float("inf")
                else:
                    logging.warning(f"Ignore edge {includer} -> {included} not found in include graph")
            elif includer in files and included == "*":
                includer_idx = files.index(includer)
                for _, included_idx in DG.out_edges(includer_idx):
                    DG[includer_idx][included_idx]["capacity"] = float("inf")
            else:
                logging.warning(f"Ignore edge {includer} -> {included} not found in include analysis")

    # Set capacity high for edges inside generated files to prevent cutting them
    # TODO - make this optional
    for edge in DG.edges():
        if files[edge[0]].startswith("out/"):
            DG[edge[0]][edge[1]]["capacity"] = float("inf")

        if files[edge[0]].startswith("build/linux/debian_bullseye_amd64-sysroot/"):
            DG[edge[0]][edge[1]]["capacity"] = float("inf")

        if files[edge[0]].startswith("third_party/llvm-build/Release+Asserts/"):
            DG[edge[0]][edge[1]]["capacity"] = float("inf")

        if files[edge[0]].startswith("third_party/abseil-cpp/"):
            DG[edge[0]][edge[1]]["capacity"] = float("inf")

        if files[edge[0]].startswith("v8/include/"):
            DG[edge[0]][edge[1]]["capacity"] = float("inf")

    # Remove everything in the graph that's not reachable from the target to speed up analysis
    reachable_nodes = set(nx.dfs_postorder_nodes(DG.reverse(), source=files.index(target)))
    DG.remove_nodes_from([idx for idx in list(DG.nodes()) if idx not in reachable_nodes])

    return DG


def compute_direct_cuts_floor(
    include_analysis: IncludeAnalysisOutput,
    DG: nx.DiGraph,
    target: str,
) -> int:
    """Compute the floor of reachable roots if all possible direct cuts are made.

    Direct cuts are edges that include the target header directly. Edges
    with infinite capacity (from ignores) are not cut.
    """
    files = include_analysis["files"]
    target_idx = files.index(target)
    DG2 = DG.copy()

    # Remove all direct includes of target that are not ignored (infinite capacity)
    for includer_idx, _ in list(DG2.in_edges(target_idx)):
        if DG2[includer_idx][target_idx].get("capacity", 1) != float("inf"):
            DG2.remove_edge(includer_idx, target_idx)

    return count_reachable_roots(include_analysis, DG2, target)


def compute_all_cuts_floor(
    include_analysis: IncludeAnalysisOutput,
    DG: nx.DiGraph,
    target: str,
) -> int:
    """Compute the floor of reachable roots if all possible cuts are made, including direct and indirect cuts.

    Indirect cuts are edges that do not directly include the target header
    but are on a path to the target. Edges with infinite capacity (from ignores)
    are not cut.
    """
    DG2 = DG.copy()

    # Remove all edges that are not ignored (infinite capacity)
    for includer_idx, included_idx in DG.edges():
        if DG2[includer_idx][included_idx].get("capacity", 1) != float("inf"):
            DG2.remove_edge(includer_idx, included_idx)

    return count_reachable_roots(include_analysis, DG2, target)


def do_minimum_edge_cut(
    DG: nx.DiGraph,
    source: int,
    target: int,
) -> Tuple[Tuple[int, int], ...]:
    """Compute the minimum edge cut between source and target using min-cut partitioning."""
    _, partition = nx.minimum_cut(DG, source, target)
    reachable, non_reachable = partition

    cutset = set()
    for u, nbrs in ((n, DG[n]) for n in reachable):
        cutset.update((u, v) for v in nbrs if v in non_reachable)

    return tuple(cutset)


def find_cuttable_sources(args):
    """Find which sources have a finite minimum cut to the target."""
    PSEUDO_NODE = 8888888888

    sources, DG, target = args
    cuttable_sources = []

    # Try cutting all sources at once first as a shortcut
    DG2 = DG.copy()
    DG2.add_node(PSEUDO_NODE)

    for source in sources:
        DG2.add_edge(PSEUDO_NODE, source, capacity=float("inf"))

    R2 = build_residual_network(DG2, "capacity")

    try:
        minimum_st_edge_cut(DG2, PSEUDO_NODE, target, residual=R2)
        return sources
    except nx.NetworkXUnbounded:
        pass

    R = build_residual_network(DG, "capacity")

    # Otherwise try each source individually
    for source in sources:
        try:
            minimum_st_edge_cut(DG, source, target, residual=R)
            cuttable_sources.append(source)
        except nx.NetworkXUnbounded:
            continue

    return cuttable_sources


def compute_top_indirect_cuts(
    include_analysis: IncludeAnalysisOutput,
    DG: nx.DiGraph,
    target: str,
    edge_dominations: dict,
) -> List[Tuple[str, str, float]]:
    """Compute the top indirect cuts ranked by prevalence of the includer file.

    Indirect cuts are edges in the minimum edge cut from all reachable roots
    to the target that do NOT directly include the target header. These are
    edges on transitive paths to the target whose removal would disconnect
    roots from reaching the target.

    Uses the same minimum edge cut approach as `minimum_edge_cut.py`:
    a pseudo-source node is connected to all cuttable reachable roots, and the
    minimum cut between the pseudo-source and the target is computed.

    Before connecting roots to the pseudo-source, each root is tested for
    cuttability (whether a finite min-cut exists to the target), following
    the `find_cuttable_sources` pattern from `minimum_edge_cut.py`.
    """
    files = include_analysis["files"]
    target_idx = files.index(target)
    total_roots = len(include_analysis["roots"])

    PSEUDO_SOURCE = 99999999

    DG2 = DG.copy()
    DG2.add_node(PSEUDO_SOURCE)

    # Find reachable roots
    reachable_nodes = set(files[idx] for idx in nx.dfs_postorder_nodes(DG2.reverse(), source=target_idx))
    reachable_roots = [
        root
        for root in include_analysis["roots"]
        if root in reachable_nodes and not root.startswith("out/") and files.index(root) != target_idx
    ]

    if not reachable_roots:
        return []

    import concurrent.futures
    from tqdm import tqdm
    from tqdm.contrib.logging import logging_redirect_tqdm

    # Filter to only cuttable sources using batched checks
    chunk_size = 8
    cuttable_count = 0

    chunked = list(batched((files.index(root) for root in reachable_roots), chunk_size))

    with logging_redirect_tqdm(), tqdm(
        disable=len(reachable_roots) == 1, total=len(reachable_roots), unit="file"
    ) as progress_output:
        with concurrent.futures.ProcessPoolExecutor() as pool:
            DG_copy = DG2.copy()

            for cuttable_sources in pool.map(
                find_cuttable_sources,
                ((chunk, DG_copy, target_idx) for chunk in chunked),
            ):
                progress_output.update(min(chunk_size, progress_output.total - progress_output.n))

                for source_idx in cuttable_sources:
                    cuttable_count += 1
                    DG2.add_edge(PSEUDO_SOURCE, source_idx, capacity=float("inf"))

        logging.debug(f"Cuttable sources: {cuttable_count} / {len(reachable_roots)}")

    if cuttable_count == 0:
        return []

    try:
        edge_cut = do_minimum_edge_cut(DG2, PSEUDO_SOURCE, target_idx)
    except nx.NetworkXUnbounded:
        return []

    # Filter to only indirect cuts (edges that do NOT point directly to the target)
    indirect_cuts = []
    for includer_idx, included_idx in edge_cut:
        if included_idx == target_idx:
            continue  # This is a direct cut, skip it
        if includer_idx == PSEUDO_SOURCE:
            continue  # Skip pseudo-node edges

        includer_file = files[includer_idx]
        included_file = files[included_idx]

        # Skip ignored edges (infinite capacity)
        if DG2[includer_idx][included_idx].get("capacity", 1) == float("inf"):
            continue

        includer_prevalence = 100.0 * (include_analysis["prevalence"][includer_file] / total_roots)
        dominated_edges = edge_dominations[(includer_file, included_file)]
        indirect_cuts.append((includer_file, included_file, includer_prevalence, dominated_edges))

    return indirect_cuts


# From analyze_includes.py in Chromium
def compute_doms(root, includes):
    """Compute the dominators for all nodes reachable from root. Node A dominates
    node B if all paths from the root to B go through A. Returns a dict from
    filename to the set of dominators of that filename (including itself).

    The implementation follows the "simple" version of Lengauer & Tarjan "A Fast
    Algorithm for Finding Dominators in a Flowgraph" (TOPLAS 1979).
    """

    parent = {}
    ancestor = {}
    vertex = []
    label = {}
    semi = {}
    pred = defaultdict(list)
    bucket = defaultdict(list)
    dom = {}

    def dfs(v):
        semi[v] = len(vertex)
        vertex.append(v)
        label[v] = v

        for w in includes[v]:
            if w not in semi:
                parent[w] = v
                dfs(w)
            pred[w].append(v)

    def compress(v):
        if ancestor[v] in ancestor:
            compress(ancestor[v])
            if semi[label[ancestor[v]]] < semi[label[v]]:
                label[v] = label[ancestor[v]]
            ancestor[v] = ancestor[ancestor[v]]

    def evaluate(v):
        if v not in ancestor:
            return v
        compress(v)
        return label[v]

    def link(v, w):
        ancestor[w] = v

    # Step 1: Initialization.
    dfs(root)

    for w in reversed(vertex[1:]):
        # Step 2: Compute semidominators.
        for v in pred[w]:
            u = evaluate(v)
            if semi[u] < semi[w]:
                semi[w] = semi[u]

        bucket[vertex[semi[w]]].append(w)
        link(parent[w], w)

        # Step 3: Implicitly define the immediate dominator for each node.
        for v in bucket[parent[w]]:
            u = evaluate(v)
            dom[v] = u if semi[u] < semi[v] else parent[w]
        bucket[parent[w]] = []

    # Step 4: Explicitly define the immediate dominator for each node.
    for w in vertex[1:]:
        if dom[w] != vertex[semi[w]]:
            dom[w] = dom[dom[w]]

    # Get the full dominator set for each node.
    all_doms = {}
    all_doms[root] = {root}

    def dom_set(node):
        if node not in all_doms:
            # node's dominators is itself and the dominators of its immediate
            # dominator.
            all_doms[node] = {node}
            all_doms[node].update(dom_set(dom[node]))

        return all_doms[node]

    return {n: dom_set(n) for n in vertex}


# From analyze_includes.py in Chromium
def compute_added_sizes(args):
    """Helper to compute added sizes from the given root."""

    roots, includes, sizes = args
    added_sizes = {node: 0 for node in includes}

    for root in roots:
        doms = compute_doms(root, includes)

        for node in doms:
            if node not in sizes:
                # Skip the (src,dst) pseudo nodes.
                continue
            for dom in doms[node]:
                added_sizes[dom] += sizes[node]

    return added_sizes


# Adapted from analyze_includes.py in Chromium
def compute_doms_to_target(include_analysis: IncludeAnalysisOutput, DG: nx.DiGraph, target: str):
    DG2 = DG.copy()
    files = include_analysis["files"]
    target_idx = files.index(target)

    # Find reachable roots
    reachable_nodes = set(files[idx] for idx in nx.dfs_postorder_nodes(DG2.reverse(), source=target_idx))
    roots = [
        root
        for root in include_analysis["roots"]
        if root in reachable_nodes and not root.startswith("out/") and files.index(root) != target_idx
    ]

    # Give each node a zero size, except for the target node
    sizes = {data["filename"]: 0 for _, data in DG.nodes(data=True) if "filename" in data}

    # Set size to be one, which means added size will effectively count the number
    # of roots that are dominated by any given edge from roots to the target node
    sizes[target] = 1

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
    parser = argparse.ArgumentParser(description="Output information about potential cuts of a target header.")
    parser.add_argument(
        "include_analysis_output",
        type=str,
        nargs="?",
        help="The include analysis output to use (can be a file path or URL). If not specified, pulls the latest.",
    )
    parser.add_argument("target", help="Target header file.")
    parser.add_argument("--ignores", action="append", default=[], help="Edges to ignore when determining cuts.")
    parser.add_argument("--skips", action="append", default=[], help="Edges to skip when determining cuts.")
    parser.add_argument("--verbose", action="store_true", default=False, help="Enable verbose logging.")
    parser.add_argument("--top", type=int, default=5, help="Number of top cuts to display (default: 5).")
    parser.add_argument(
        "--sort-by",
        choices=["prevalence", "dominated"],
        default="prevalence",
        help="Sort results by prevalence (default) or dominated edges count.",
    )
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

    if args.target not in include_analysis["files"]:
        print(f"error: {args.target} is not a known file")
        return 1

    ignores: Set[Tuple[str, str]] = set()
    skips: Set[Tuple[str, str]] = set()

    for ignores_file in args.ignores:
        with open(ignores_file, "r", newline="") as f:
            ignores.update([tuple(row) for row in csv.reader(f) if row])

    for skips_file in args.skips:
        with open(skips_file, "r", newline="") as f:
            skips.update(
                [tuple(row) for row in csv.reader(f) if row and row[0].strip() and not row[0].startswith("#")]
            )

    total_roots = len(include_analysis["roots"])

    # Remaining: reachable roots after skips are applied
    DG = create_include_graph(include_analysis, args.target, ignores=tuple(ignores), skips=tuple(skips))
    remaining_reachable = count_reachable_roots(include_analysis, DG, args.target)

    # Get original prevalence for the target header (reachable roots without any skips)
    DG_original = create_graph_from_include_analysis(include_analysis)
    original_reachable = count_reachable_roots(include_analysis, DG_original, args.target)

    if original_reachable == 0:
        remaining_pct = 0.0
    else:
        remaining_pct = 100.0 * remaining_reachable / original_reachable

    remaining_prevalence = 100.0 * remaining_reachable / total_roots

    # Direct cuts floor
    direct_floor = compute_direct_cuts_floor(include_analysis, DG, args.target)

    if original_reachable == 0:
        direct_floor_pct = 0.0
    else:
        direct_floor_pct = 100.0 * direct_floor / original_reachable

    direct_floor_prevalence = 100.0 * direct_floor / total_roots

    # All cuts floor
    all_cuts_floor = compute_all_cuts_floor(include_analysis, DG, args.target)

    if original_reachable == 0:
        all_cuts_floor_pct = 0.0
    else:
        all_cuts_floor_pct = 100.0 * all_cuts_floor / original_reachable

    all_cuts_floor_prevalence = 100.0 * all_cuts_floor / total_roots

    # Root direct includes floor: count of roots that directly include the target
    roots_set = set(include_analysis["roots"])
    root_direct_includes_floor = sum(
        1 for includer in include_analysis["included_by"].get(args.target, []) if includer in roots_set
    )

    if original_reachable == 0:
        root_direct_includes_floor_pct = 0.0
    else:
        root_direct_includes_floor_pct = 100.0 * root_direct_includes_floor / original_reachable

    root_direct_includes_floor_prevalence = 100.0 * root_direct_includes_floor / total_roots

    original_prevalence = 100.0 * original_reachable / total_roots
    remaining_delta = remaining_prevalence - original_prevalence
    direct_floor_delta = direct_floor_prevalence - original_prevalence
    all_cuts_floor_delta = all_cuts_floor_prevalence - original_prevalence
    root_direct_includes_floor_delta = root_direct_includes_floor_prevalence - original_prevalence

    # Output to stderr
    if remaining_delta:
        print(
            f"Remaining: {remaining_pct:.2f}% ({remaining_prevalence:.2f}% prevalence, {remaining_delta:+.2f}%%)",
            file=sys.stderr,
        )
    else:
        print(f"Remaining: {remaining_pct:.2f}% ({remaining_prevalence:.2f}% prevalence)", file=sys.stderr)
    print(
        f"Only direct cuts floor: {direct_floor_pct:.2f}% ({direct_floor_prevalence:.2f}% prevalence, {direct_floor_delta:+.2f}%%)",
        file=sys.stderr,
    )
    print(
        f"All cuts floor: {all_cuts_floor_pct:.2f}% ({all_cuts_floor_prevalence:.2f}% prevalence, {all_cuts_floor_delta:+.2f}%%)",
        file=sys.stderr,
    )
    print(
        f"Root direct includes floor: {root_direct_includes_floor_pct:.2f}% ({root_direct_includes_floor_prevalence:.2f}% prevalence, {root_direct_includes_floor_delta:+.2f}%%)",
        file=sys.stderr,
    )

    edge_dominations = compute_doms_to_target(include_analysis, DG, args.target)

    # Compute top N direct cuts ranked by prevalence of the includer file
    files = include_analysis["files"]
    target_idx = files.index(args.target)
    direct_includers = []

    for includer_idx, _ in DG.in_edges(target_idx):
        # Skip ignored edges (infinite capacity)
        if DG[includer_idx][target_idx].get("capacity", 1) == float("inf"):
            continue

        includer_file = files[includer_idx]
        includer_prevalence = 100.0 * (include_analysis["prevalence"][includer_file] / total_roots)
        direct_includers.append(
            (includer_file, args.target, includer_prevalence, edge_dominations[(includer_file, args.target)])
        )

    # Sort and take top N
    sort_key = (lambda x: x[3]) if args.sort_by == "dominated" else (lambda x: x[2])
    direct_includers.sort(key=sort_key, reverse=True)
    top_direct = direct_includers[: args.top]

    print(f"\nTop {args.top} direct includers (by {args.sort_by})")
    writer = csv.writer(sys.stdout)
    for includer_file, included_file, prevalence, dominated_edges in top_direct:
        writer.writerow([includer_file, included_file, f"{prevalence:.2f}", dominated_edges])

    # Compute top N indirect cuts
    all_indirect = compute_top_indirect_cuts(include_analysis, DG, args.target, edge_dominations)
    all_indirect.sort(key=sort_key, reverse=True)
    top_indirect = all_indirect[: args.top]

    print(f"\nTop {args.top} indirect cuts (by {args.sort_by})")
    for includer_file, included_file, prevalence, dominated_edges in top_indirect:
        writer.writerow([includer_file, included_file, f"{prevalence:.2f}", dominated_edges])

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass  # Don't show the user anything
