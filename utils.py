import multiprocessing
import os
import re
from typing import List

from include_analysis import IncludeAnalysisOutput


def get_worker_count():
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return multiprocessing.cpu_count()


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
                    break

            for include in includes:
                include = generated_file_prefix.match(include).group(1)
                edge_sizes[filename][include] = size

    return edge_sizes
