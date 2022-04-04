#!/usr/bin/env python3

import argparse
import json
import sys

# TODO - Is this flag the same on Windows?
TRACE_INCLUDES_FLAG = "-H"


def post_process_compilation_database(compilation_database: list):
    """
    Post-process the compilation database

    This filters out native client compilation entries for filenames so that clangd
    will see the normal compilation for this system, and also strips out the trace
    includes flag out of the compile command so that verbose output from clangd
    won't be dominated by the includes tracing.

    """
    return [
        {
            **entry,
            "command": " ".join(entry["command"].split(f" {TRACE_INCLUDES_FLAG} "))
            if f" {TRACE_INCLUDES_FLAG} " in entry["command"]
            else entry["command"],
        }
        for entry in compilation_database
        if "native_client" not in entry["command"]
    ]


def main():
    parser = argparse.ArgumentParser(description="Post-process the clang compilation database for analysis")
    parser.add_argument(
        "compilation_database",
        type=argparse.FileType("r"),
        help="The JSON compilation database to post-process (- for stdin).",
    )
    args = parser.parse_args()

    try:
        compilation_database = json.loads(args.compilation_database.read())
    except json.JSONDecodeError:
        print("error: Couldn't parse JSON compilation database")
        return 1

    post_processed = post_process_compilation_database(compilation_database)

    # Simply write the post-processed database to stdout
    json.dump(post_processed, sys.stdout)

    return 0


if __name__ == "__main__":
    sys.exit(main())
