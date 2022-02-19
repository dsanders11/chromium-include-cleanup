import multiprocessing
import os
import re

from include_analysis import IncludeAnalysisOutput


def get_worker_count():
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return multiprocessing.cpu_count()


def get_edge_sizes(include_analysis: IncludeAnalysisOutput):
    # Strip off the path prefix for generated file includes so matching will work
    generated_file_prefix = re.compile(r"^(?:out/\w+/gen/)?(.*)$")

    return {
        filename: {
            generated_file_prefix.match(included).group(1): size
            for included, size in include_analysis["esizes"][filename].items()
        }
        for filename in include_analysis["esizes"]
    }
