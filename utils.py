import functools
import logging
import multiprocessing
import os
import pathlib
import re
from collections import defaultdict
from typing import DefaultDict, Dict, List

import networkx as nx

from common import Configuration
from include_analysis import IncludeAnalysisOutput


def get_worker_count():
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return multiprocessing.cpu_count()


def load_config(name: str):
    config_file = pathlib.Path(__file__).parent.joinpath("configs", name).with_suffix(".json")

    if not config_file.exists():
        print(f"error: no config file found: {config_file}")
        return 1

    config = Configuration.parse_file(config_file)

    # TODO - Make it recursive so it can handle deeply nested configs
    # TODO - Maybe warn about duplicates when merging?
    # Merge the dependencies (maybe this should be made generic to sub-configs?)
    for dependency in config.dependencies:
        # Dependency paths are automatically added to include directories
        config.includeDirs.append(dependency)

        if isinstance(config.dependencies[dependency], str):
            dependency_config_file = config_file.parent.joinpath(config.dependencies[dependency])

            if not dependency_config_file.exists():
                print(f"error: no config file found: {dependency_config_file}")
                return 1

            dependency_config = Configuration.parse_file(dependency_config_file)
            dependency_ignores = dependency_config.ignores
        else:
            dependency_ignores = config.dependencies[dependency].ignores

        # Files to skip are relative to the source root
        for file_to_skip in dependency_ignores.skip:
            config.ignores.skip.append(str(pathlib.Path(dependency).joinpath(file_to_skip)))

        for op in ("add", "remove"):
            # Filenames are relative to the source root
            for filename in getattr(dependency_ignores, op).filenames:
                getattr(config.ignores, op).filenames.append(str(pathlib.Path(dependency).joinpath(filename)))

            # Headers are accessible both internally and externally, so include them as-is and
            # also include them relative to the source root for top-level inclusion
            for header in getattr(dependency_ignores, op).headers:
                headers = getattr(config.ignores, op).headers
                headers.append(header)
                headers.append(str(pathlib.Path(dependency).joinpath(header)))

            # Edges are only processed if their file is, and that file is relative to the source root
            for filename, header in getattr(dependency_ignores, op).edges:
                getattr(config.ignores, op).edges.append((str(pathlib.Path(dependency).joinpath(filename)), header))

    return config


def get_include_analysis_edge_sizes(include_analysis: IncludeAnalysisOutput):
    edge_sizes: DefaultDict[str, Dict[str, int]] = defaultdict(dict)

    for filename in include_analysis["esizes"]:
        for include, size in include_analysis["esizes"][filename].items():
            edge_sizes[filename][include] = size

    return edge_sizes


def get_include_analysis_edge_expanded_sizes(include_analysis: IncludeAnalysisOutput):
    files = include_analysis["files"]
    edge_expanded_sizes: DefaultDict[str, Dict[str, int]] = defaultdict(dict)

    for filename in files:
        for include in include_analysis["includes"][filename]:
            try:
                edge_expanded_sizes[filename][include] = include_analysis["tsizes"][include]
            except KeyError:
                pass

    return edge_expanded_sizes


def get_include_analysis_edge_file_sizes(include_analysis: IncludeAnalysisOutput):
    if "_edge_file_sizes" in include_analysis:
        return include_analysis["_edge_file_sizes"]

    files = include_analysis["files"]
    edge_file_sizes: DefaultDict[str, Dict[str, int]] = defaultdict(dict)

    for filename in files:
        for include in include_analysis["includes"][filename]:
            try:
                edge_file_sizes[filename][include] = include_analysis["sizes"][include]
            except KeyError:
                logging.warning(f"Couldn't get include file size for {include}")
                pass

    include_analysis["_edge_file_sizes"] = edge_file_sizes

    return edge_file_sizes


def get_include_file_size(include_analysis: IncludeAnalysisOutput, include: str):
    try:
        return include_analysis["sizes"][include]
    except KeyError:
        logging.warning(f"Couldn't get include file size for {include}")
        include_analysis["sizes"][include] = 0
        return 0


def get_include_analysis_edge_prevalence(include_analysis: IncludeAnalysisOutput):
    files = include_analysis["files"]
    root_count = len(include_analysis["roots"])
    edge_prevalence: DefaultDict[str, Dict[str, float]] = defaultdict(dict)

    for filename in files:
        for include in include_analysis["includes"][filename]:
            prevalence = include_analysis["prevalence"][filename]
            edge_prevalence[filename][include] = (100.0 * prevalence) / root_count

    return edge_prevalence


def create_graph_from_include_analysis(include_analysis: IncludeAnalysisOutput):
    DG = nx.DiGraph()

    files = include_analysis["files"]
    file_idx_lookup = {filename: idx for idx, filename in enumerate(files)}

    # Add nodes and edges to the graph
    for idx, filename in enumerate(files):
        DG.add_node(idx, filename=filename)

        for include in include_analysis["includes"][filename]:
            DG.add_edge(idx, file_idx_lookup[include])

    return DG


def get_include_analysis_edges_centrality(include_analysis: IncludeAnalysisOutput):
    DG: nx.DiGraph = create_graph_from_include_analysis(include_analysis)
    nodes_in = nx.in_degree_centrality(DG)
    nodes_out = nx.out_degree_centrality(DG)

    files = include_analysis["files"]
    file_idx_lookup = {filename: idx for idx, filename in enumerate(files)}
    edges_centrality: DefaultDict[str, Dict[str, float]] = defaultdict(dict)

    # Centrality is a metric for a node, but we want to create a metric for an edge.
    # For the moment, this will use a heuristic which combines the in-degree centrality
    # of the node where the edge starts, and the out-degree centrality of the node the
    # edge is pulling into the graph. This hopefully creates a metric which lets us find
    # edges in commonly included nodes, which pull lots of nodes into the graph.
    for idx, filename in enumerate(files):
        for include in include_analysis["includes"][filename]:
            # Scale the value up so it's more human-friendly instead of having lots of leading zeroes
            edges_centrality[filename][include] = 100000 * nodes_out[file_idx_lookup[include]] * nodes_in[idx]

    return edges_centrality


def get_include_analysis_edge_includer_size(include_analysis: IncludeAnalysisOutput):
    edge_sizes: DefaultDict[str, Dict[str, int]] = defaultdict(dict)

    for filename in include_analysis["esizes"]:
        for include, _ in include_analysis["esizes"][filename].items():
            edge_sizes[filename][include] = include_analysis["sizes"][filename]

    return edge_sizes


@functools.cache
def _init_path(value: str):
    return pathlib.Path(value)


# Cache for normalized include paths to avoid repeated normalization
_normalized_include_paths: Dict[str, str] = {}


def normalize_include_path(
    include_analysis: IncludeAnalysisOutput, includer: str, include: str, include_directories: List[str] = None
) -> str:
    """Normalize an include path to long form (e.g. full file path)."""

    if include not in _normalized_include_paths:
        # TODO - Make include_analysis["files"] a set always, after verifying no existing code relies on it being a list
        if "files_set" not in include_analysis:
            include_analysis["files_set"] = set(include_analysis["files"])

        files = include_analysis["files_set"]
        normalized = None

        if include_directories is None:
            include_directories = []

        # Normalization may not be necessary
        if include in files:
            normalized = include
        else:
            if include.startswith("<") and include.endswith(">"):
                include = include.strip("<>")

                # Angle bracket headers might be in either libc++ or the sysroot
                sysroot = _init_path(include_analysis["sysroot"])
                normalized = f"third_party/libc++/src/include/{include}"

                if normalized not in files:
                    normalized = str(sysroot.joinpath("usr", "include", include))

                if normalized not in files:
                    normalized = str(sysroot.joinpath("usr", "include", include_analysis["sysroot_platform"], include))

                if normalized not in files:
                    for include_directory in include_directories:
                        # Replace {sysroot} and {sysroot-platform} with the actual path from the include analysis
                        if include_directory.startswith("{sysroot}"):
                            resolved_include_directory = str(sysroot.joinpath(include_directory[10:]))

                            full_path = str(_init_path(resolved_include_directory).joinpath(include))

                            if full_path in files:
                                normalized = full_path
                                break
                        elif include_directory.startswith("{sysroot-platform}"):
                            for part in ["include", "lib"]:
                                resolved_include_directory = str(
                                    sysroot.joinpath(
                                        "usr", part, include_analysis["sysroot_platform"], include_directory[19:]
                                    )
                                )

                                full_path = str(_init_path(resolved_include_directory).joinpath(include))

                                if full_path in files:
                                    normalized = full_path
                                    break

                            if normalized in files:
                                break
            else:
                # First check if it might be a relative file
                relative_file_path = str(_init_path(includer).parent.joinpath(include))

                if relative_file_path in files:
                    normalized = relative_file_path
                else:
                    # Then check if it might be a generated file
                    gen_prefix = _init_path(include_analysis["gen_prefix"])
                    generated_file_path = str(gen_prefix.joinpath(include))

                    if generated_file_path in files:
                        normalized = generated_file_path
                    else:

                        # If not, try to find it in the include directories
                        for include_directory in include_directories:
                            if include_directory.startswith("{gen}"):
                                # Replace {gen} with the actual gen prefix from the include analysis
                                include_directory = str(gen_prefix.joinpath(include_directory[6:]))
                            elif include_directory.startswith("{sysroot}") or include_directory.startswith(
                                "{sysroot-platform}"
                            ):
                                continue  # These are handled as angle brackets

                            full_path = str(_init_path(include_directory).joinpath(include))

                            if full_path in files:
                                normalized = full_path
                                break

            if normalized is None:
                logging.warning(f"Could not normalize include path: {include}.")
                normalized = include
            elif normalized not in files:
                logging.warning(
                    f"Normalized include path not found in include analysis files: {normalized}. Falling back to original include path."
                )
                normalized = include

        _normalized_include_paths[include] = normalized

    return _normalized_include_paths[include]
