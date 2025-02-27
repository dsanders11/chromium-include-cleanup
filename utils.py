import multiprocessing
import os
import pathlib
import re
from collections import defaultdict
from typing import DefaultDict, Dict, List

import networkx as nx

from common import Configuration
from include_analysis import IncludeAnalysisOutput

# Strip off the path prefix for generated file includes so matching will work
GENERATED_FILE_PREFIX_REGEX = re.compile(r"^(?:out/\w+/gen/)?(.*)$")


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


def get_include_analysis_edge_sizes(include_analysis: IncludeAnalysisOutput, include_directories: List[str] = None):
    edge_sizes = {}

    if include_directories is None:
        include_directories = []

    for filename in include_analysis["esizes"]:
        edge_sizes[filename] = {}

        for include, size in include_analysis["esizes"][filename].items():
            includes = [include]

            # If an include is in an include directory, strip that prefix and add it to edge sizes for matching
            for include_directory in include_directories:
                include_directory = include_directory if include_directory.endswith("/") else f"{include_directory}/"
                if include.startswith(include_directory):
                    includes.append(include[len(include_directory) :])

            for include in includes:
                include = GENERATED_FILE_PREFIX_REGEX.match(include).group(1)
                edge_sizes[filename][include] = size

    return edge_sizes


def get_include_analysis_edge_expanded_sizes(
    include_analysis: IncludeAnalysisOutput, include_directories: List[str] = None
):
    files = include_analysis["files"]
    edge_expanded_sizes: DefaultDict[str, Dict[str, int]] = defaultdict(dict)

    if include_directories is None:
        include_directories = []

    for filename in files:
        for include in include_analysis["includes"][filename]:
            includes = [include]

            # If an include is in an include directory, strip that prefix and add it for matching
            for include_directory in include_directories:
                include_directory = include_directory if include_directory.endswith("/") else f"{include_directory}/"
                if include.startswith(include_directory):
                    includes.append(include[len(include_directory) :])

            for include in includes:
                include = GENERATED_FILE_PREFIX_REGEX.match(include).group(1)
                try:
                    edge_expanded_sizes[filename][include] = include_analysis["tsizes"][include]
                except KeyError:
                    pass

    return edge_expanded_sizes


def get_include_analysis_edge_prevalence(
    include_analysis: IncludeAnalysisOutput, include_directories: List[str] = None
):
    files = include_analysis["files"]
    root_count = len(include_analysis["roots"])
    edge_prevalence: DefaultDict[str, Dict[str, float]] = defaultdict(dict)

    if include_directories is None:
        include_directories = []

    for filename in files:
        for include in include_analysis["includes"][filename]:
            includes = [include]

            # If an include is in an include directory, strip that prefix and add it for matching
            for include_directory in include_directories:
                include_directory = include_directory if include_directory.endswith("/") else f"{include_directory}/"
                if include.startswith(include_directory):
                    includes.append(include[len(include_directory) :])

            for include in includes:
                include = GENERATED_FILE_PREFIX_REGEX.match(include).group(1)
                edge_prevalence[filename][include] = (100.0 * include_analysis["prevalence"][filename]) / root_count

    return edge_prevalence


def create_graph_from_include_analysis(include_analysis: IncludeAnalysisOutput):
    DG = nx.DiGraph()

    files = include_analysis["files"]

    # Add nodes and edges to the graph
    # XXX - Unfortunately this is pretty slow, takes several minutes to add the edges
    for idx, filename in enumerate(files):
        DG.add_node(idx, filename=filename)

        for include in include_analysis["includes"][filename]:
            DG.add_edge(idx, files.index(include))

    return DG


def get_include_analysis_edges_centrality(
    include_analysis: IncludeAnalysisOutput, include_directories: List[str] = None
):
    DG: nx.DiGraph = create_graph_from_include_analysis(include_analysis)
    nodes_in = nx.in_degree_centrality(DG)
    nodes_out = nx.out_degree_centrality(DG)

    files = include_analysis["files"]
    edges_centrality: DefaultDict[str, Dict[str, float]] = defaultdict(dict)

    if include_directories is None:
        include_directories = []

    # Centrality is a metric for a node, but we want to create a metric for an edge.
    # For the moment, this will use a heuristic which combines the in-degree centrality
    # of the node where the edge starts, and the out-degree centrality of the node the
    # edge is pulling into the graph. This hopefully creates a metric which lets us find
    # edges in commonly included nodes, which pull lots of nodes into the graph.
    for idx, filename in enumerate(files):
        for absolute_include in include_analysis["includes"][filename]:
            includes = [absolute_include]

            # If an include is in an include directory, strip that prefix and add it for matching
            for include_directory in include_directories:
                include_directory = include_directory if include_directory.endswith("/") else f"{include_directory}/"
                if absolute_include.startswith(include_directory):
                    includes.append(absolute_include[len(include_directory) :])

            for include in includes:
                include = GENERATED_FILE_PREFIX_REGEX.match(include).group(1)

                # Scale the value up so it's more human-friendly instead of having lots of leading zeroes
                edges_centrality[filename][include] = 100000 * nodes_out[files.index(absolute_include)] * nodes_in[idx]

    return edges_centrality
