#!/usr/bin/env python3

import argparse
import csv
import logging
import os
import sys
from itertools import batched
from urllib.request import HTTPError

import networkx as nx
from networkx.algorithms.centrality import betweenness_centrality_subset
from networkx.algorithms.connectivity import minimum_st_edge_cut
from networkx.algorithms.flow import build_residual_network

from include_analysis import IncludeAnalysisOutput, ParseError, load_include_analysis
from replace_include_with_forward_decl import replace_include_with_forward_decl
from typing import DefaultDict, Dict, Iterator, Optional, Tuple
from utils import create_graph_from_include_analysis, get_include_analysis_edge_prevalence

CUTTABLE = "cuttable"
UNCUTTABLE = "uncuttable"
FULL_CUT = "full_cut"


def create_include_graph(
    include_analysis: IncludeAnalysisOutput,
    ignores: Optional[Tuple[Tuple[str, str]]],
    skips: Optional[Tuple[Tuple[str, str]]],
) -> nx.DiGraph:
    DG: nx.DiGraph = create_graph_from_include_analysis(include_analysis)

    if skips:
        for includer, included in skips:
            if includer in include_analysis["files"] and included in include_analysis["files"]:
                includer_idx = include_analysis["files"].index(includer)
                included_idx = include_analysis["files"].index(included)

                if DG.has_edge(includer_idx, included_idx):
                    DG.remove_edge(includer_idx, included_idx)

    if ignores:
        # Set capacity high for ignored edges to prevent cutting them
        for includer, included in ignores:
            if includer in include_analysis["files"] and included in include_analysis["files"]:
                includer_idx = include_analysis["files"].index(includer)
                included_idx = include_analysis["files"].index(included)

                if DG.has_edge(includer_idx, included_idx):
                    DG[includer_idx][included_idx]["capacity"] = float("inf")

    # Set capacity high for edges inside generated files to prevent cutting them
    for edge in DG.edges():
        if include_analysis["files"][edge[0]].startswith("out/"):
            DG[edge[0]][edge[1]]["capacity"] = float("inf")

    return DG


def find_cuttable_sources(args):
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


# TODO - Unify sources logic and make `start_from_source_includes` just set the
#        edges from source to its includes with infinite capacity so they can't be cut?
def minimum_edge_cut(
    include_analysis: IncludeAnalysisOutput,
    DG: nx.DiGraph,
    source: str,
    target: str,
    start_from_source_includes=False,
    prevalence_threshold: float = None,
    ignores: Optional[Tuple[Tuple[str, str]]] = None,
    skips: Optional[Tuple[Tuple[str, str]]] = None,
) -> Iterator[Tuple[str, str]]:
    files = include_analysis["files"]
    edge_prevalence = get_include_analysis_edge_prevalence(include_analysis)

    pseudo_target_node = 77777777

    if "," in target:
        # Create a pseudo-node that is included by all targets
        target_node = pseudo_target_node
        DG.add_node(target_node)

        # Add the edges and make them uncuttable
        for target_file in target.split(","):
            DG.add_edge(files.index(target_file), target_node, capacity=float("inf"))
    else:
        target_node = files.index(target)

    if source == "*":
        DG2 = DG.reverse()

        # Only test from source nodes that can reach the target
        reachable_nodes = set(
            [
                files[idx]
                for idx in nx.dfs_postorder_nodes(DG2, source=target_node)
                if idx != pseudo_target_node
            ]
        )

        # Remove everything in the graph that's not reachable from the target to speed up analysis
        DG.remove_nodes_from([
            files.index(node) for node in include_analysis["files"] if node not in reachable_nodes
        ])

        reachable_roots = set(include_analysis["roots"]).intersection(reachable_nodes)

        if start_from_source_includes:
            sources = set(
                [source for root in include_analysis["roots"] for source in include_analysis["includes"][root]]
            ).intersection(reachable_nodes)
            start_from_source_includes = False
        else:
            sources = reachable_roots
    elif "," in source:
        sources = source.split(",")
    else:
        sources = [source]

    from tqdm import tqdm
    from tqdm.contrib.logging import logging_redirect_tqdm

    # Create a pseudo-node that includes all sources
    DG.add_node(99999999)

    sources = [source for source in sources if not source.startswith("out/") and source != target]

    import concurrent.futures

    chunk_size = 8
    chunked = list(batched((files.index(source) for source in sources), chunk_size))

    # TODO - Return this count so we can output how many roots are uncuttable
    cuttable_sources_count = 0

    with logging_redirect_tqdm(), tqdm(disable=len(sources) == 1, total=len(sources), unit="file") as progress_output:
        with concurrent.futures.ProcessPoolExecutor() as pool:
            DG2 = DG.copy()

            for cuttable_sources in pool.map(
                find_cuttable_sources,
                ((chunk, DG2, target_node) for chunk in chunked),
            ):
                progress_output.update(min(chunk_size, progress_output.total - progress_output.n))

                for source in cuttable_sources:
                    cuttable_sources_count += 1
                    DG.add_edge(99999999, source, capacity=float("inf"))

    # Build residual network
    R = build_residual_network(DG, "capacity")

    # Do the cut
    edge_cut = minimum_st_edge_cut(DG, 99999999, target_node, residual=R)

    node_centrality = betweenness_centrality_subset(DG, sources=[99999999], targets=[target_node])

    for includer_idx, include_idx in edge_cut:
        includer = files[includer_idx]
        included = files[include_idx]
        prevalence = edge_prevalence[includer][included]

        yield includer, included, prevalence, node_centrality[includer_idx]


def minimum_edge_cut_with_auto_fwd_decl(
    include_analysis: IncludeAnalysisOutput,
    DG: nx.DiGraph,
    source: str,
    target: str,
    includer: str,
    included: str,
) -> Iterator[Tuple[str, ...]]:
    files = include_analysis["files"]
    R = build_residual_network(DG, "capacity")

    edge_prevalence = get_include_analysis_edge_prevalence(include_analysis)

    # start_from_source_includes is always true in auto forward declaration mode if source is not a header
    sources = include_analysis["includes"][source] if source == includer and not source.endswith(".h") else [includer]

    full_cut_successful = True

    for source in sources:
        # With the start_from_source_includes option, the included could be a direct include
        if source == included:
            logging.warning(f"{included} is a direct include of {source}")
            continue

        edge_cut = minimum_st_edge_cut(DG, files.index(source), files.index(included), residual=R)

        for includer_idx, include_idx in edge_cut:
            edge_includer = files[includer_idx]
            edge_included = files[include_idx]

            # Check if the include can be replaced with a forward declaration
            result = replace_include_with_forward_decl(include_analysis, edge_includer, edge_included)
            prevalence = edge_prevalence[edge_includer][edge_included]

            if result["can_replace_include"]:
                logging.info(f"Can replace include {edge_includer} -> {edge_included} using fwd decl")
                yield (CUTTABLE, edge_includer, edge_included, prevalence)
            else:
                logging.info(f"Could not replace include {edge_includer} -> {edge_included} using fwd decl")
                yield (UNCUTTABLE, edge_includer, edge_included, prevalence)

                # Try going upstream to see if we can cut it there
                if edge_includer != source:
                    upstream_cuts_successful = True

                    for result in minimum_edge_cut_with_auto_fwd_decl(
                        include_analysis,
                        DG,
                        source,
                        target,
                        source,
                        edge_includer,
                    ):
                        if result[0] == FULL_CUT:
                            upstream_cuts_successful = result[1]
                        else:
                            yield result

                    # If successful, move on to the next edge
                    if upstream_cuts_successful:
                        continue

                # Otherwise try going downstream to see if we can cut it there
                if edge_included != target:
                    downstream_cuts_successful = True

                    for result in minimum_edge_cut_with_auto_fwd_decl(
                        include_analysis,
                        DG,
                        source,
                        target,
                        edge_included,
                        target,
                    ):
                        if result[0] == FULL_CUT:
                            downstream_cuts_successful = result[1]
                        else:
                            yield result

                    # If successful, move on to the next edge
                    if downstream_cuts_successful:
                        continue

                # If we get here, we couldn't cut this edge
                full_cut_successful = False

    # Yield the final result
    yield (FULL_CUT, full_cut_successful)


def main():
    parser = argparse.ArgumentParser(description="Find the minimum edge cut between two files")
    parser.add_argument(
        "include_analysis_output",
        type=str,
        nargs="?",
        help="The include analysis output to use (can be a file path or URL). If not specified, pulls the latest.",
    )
    parser.add_argument("source", help="Source file.")
    parser.add_argument("target", help="Target file.")
    parser.add_argument(
        "--auto-fwd-decl", action="store_true", default=False, help="Enable automatic forward declaration mode."
    )
    parser.add_argument(
        "--start-from-source-includes",
        action="store_true",
        help="Start from includes of the source file, rather than the source file itself.",
    )
    parser.add_argument(
        "--prevalence-threshold", type=float, help="Filter out edges with a prevalence percentage below the threshold."
    )
    parser.add_argument("--ignores", help="Edges to ignore when determining cuts.")
    parser.add_argument("--skips", help="Edges to skip when determining cuts.")
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

    csv_writer = csv.writer(sys.stdout)

    if args.source != "*" and args.source not in include_analysis["files"]:
        print(f"error: {args.source} is not a known file")
        return 1

    for target in args.target.split(","):
        if target not in include_analysis["files"]:
            print(f"error: {target} is not a known file")
            return 1

    if args.auto_fwd_decl and args.prevalence_threshold is not None:
        print("error: --auto-fwd-decl and --prevalence-threshold cannot be used together")
        return 1

    ignores = None
    skips = None

    if args.ignores:
        with open(args.ignores, "r", newline="") as f:
            ignores: Tuple[Tuple[str, str]] = [row for row in csv.reader(f) if row]

    if args.skips:
        with open(args.skips, "r", newline="") as f:
            skips: Tuple[Tuple[str, str]] = [row for row in csv.reader(f) if row]

    DG = create_include_graph(include_analysis, ignores=ignores, skips=skips)

    try:
        if args.auto_fwd_decl:
            full_cut_successful = True
            cuts_seen = set()

            for result in minimum_edge_cut_with_auto_fwd_decl(
                include_analysis,
                DG,
                args.source,
                args.target,
                args.source,
                args.target,
            ):
                if result[0] == FULL_CUT:
                    full_cut_successful = result[1]
                else:
                    if result not in cuts_seen:
                        csv_writer.writerow(result)

                    cuts_seen.add(result)

            sys.stdout.flush()

            if not full_cut_successful:
                print(
                    f"error: could not cut all edges between {args.source} and {args.target} using forward declarations"
                )
                return 4
        else:
            for row in minimum_edge_cut(
                include_analysis,
                DG,
                args.source,
                args.target,
                start_from_source_includes=args.start_from_source_includes,
                prevalence_threshold=args.prevalence_threshold,
                ignores=ignores,
                skips=skips,
            ):
                csv_writer.writerow(row)

            sys.stdout.flush()
    except nx.NetworkXUnbounded:
        print(f"error: can't fully cut path from {args.source} to {args.target}")
        return 6
    except nx.NetworkXNoPath:
        print(f"error: no transitive include path from {args.source} to {args.target}")
        return 3
    except HTTPError as e:
        print(f"error: HTTP error {e.code} fetching file content for {e.url}")
        return 5
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
