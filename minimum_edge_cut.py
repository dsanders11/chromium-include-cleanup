#!/usr/bin/env python3

import argparse
import csv
import logging
import os
import sys
from urllib.request import HTTPError

import networkx as nx

from include_analysis import IncludeAnalysisOutput, ParseError, load_include_analysis
from replace_include_with_forward_decl import replace_include_with_forward_decl
from typing import Iterator, Tuple
from utils import create_graph_from_include_analysis, get_include_analysis_edge_prevalence

CUTTABLE = "cuttable"
UNCUTTABLE = "uncuttable"
FULL_CUT = "full_cut"


def minimum_edge_cut(
    include_analysis: IncludeAnalysisOutput,
    source: str,
    target: str,
    start_from_source_includes=False,
    prevalence_threshold: float = None,
) -> Iterator[Tuple[str, str]]:
    files = include_analysis["files"]
    DG: nx.DiGraph = create_graph_from_include_analysis(include_analysis)

    edge_prevalence = get_include_analysis_edge_prevalence(include_analysis)

    sources = include_analysis["includes"][source] if start_from_source_includes else [source]
    cuts = []

    for source in sources:
        # With the start_from_source_includes option, the target could be a direct include
        if source == target:
            logging.warning(f"{target} is a direct include of {source}")
            continue

        edge_cut = nx.minimum_edge_cut(DG, files.index(source), files.index(target))

        for includer_idx, include_idx in edge_cut:
            includer = files[includer_idx]
            included = files[include_idx]

            if start_from_source_includes:
                cuts.append((includer, included))
            else:
                prevalence = edge_prevalence[includer][included]

                if prevalence_threshold and prevalence < prevalence_threshold:
                    continue

                yield includer, included, prevalence

    if start_from_source_includes:
        # Deduplicate cuts
        for includer, included in set(cuts):
            prevalence = edge_prevalence[includer][included]

            if prevalence_threshold and prevalence < prevalence_threshold:
                continue

            yield includer, included, prevalence


def minimum_edge_cut_with_auto_fwd_decl(
    include_analysis: IncludeAnalysisOutput,
    source: str,
    target: str,
    includer: str,
    included: str,
) -> Iterator[Tuple[str, ...]]:
    files = include_analysis["files"]
    DG: nx.DiGraph = create_graph_from_include_analysis(include_analysis)

    edge_prevalence = get_include_analysis_edge_prevalence(include_analysis)

    # start_from_source_includes is always true in auto forward declaration mode if source is not a header
    sources = include_analysis["includes"][source] if source == includer and not source.endswith(".h") else [includer]

    full_cut_successful = True

    for source in sources:
        # With the start_from_source_includes option, the included could be a direct include
        if source == included:
            logging.warning(f"{included} is a direct include of {source}")
            continue

        edge_cut = nx.minimum_edge_cut(DG, files.index(source), files.index(included))

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

    if args.source not in include_analysis["files"]:
        print(f"error: {args.source} is not a known file")
        return 1

    if args.target not in include_analysis["files"]:
        print(f"error: {args.target} is not a known file")
        return 1

    if args.auto_fwd_decl and args.prevalence_threshold is not None:
        print("error: --auto-fwd-decl and --prevalence-threshold cannot be used together")
        return 1

    try:
        if args.auto_fwd_decl:
            full_cut_successful = True
            cuts_seen = set()

            for result in minimum_edge_cut_with_auto_fwd_decl(
                include_analysis,
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
                args.source,
                args.target,
                start_from_source_includes=args.start_from_source_includes,
                prevalence_threshold=args.prevalence_threshold,
            ):
                csv_writer.writerow(row)

            sys.stdout.flush()
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
