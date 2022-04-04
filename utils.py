import multiprocessing
import os
import pathlib
import re
from typing import List

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


def get_edge_sizes(include_analysis: IncludeAnalysisOutput, include_directories: List[str] = None):
    # Strip off the path prefix for generated file includes so matching will work
    generated_file_prefix = re.compile(r"^(?:out/\w+/gen/)?(.*)$")

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
                include = generated_file_prefix.match(include).group(1)
                edge_sizes[filename][include] = size

    return edge_sizes
