#!/usr/bin/env python3

import argparse
import json
import sys


def post_process_compilation_database(compilation_database: list):
    """
    Post-process the compilation database

    For the moment all this does is filter out native client compilation entries
    for filenames so that clangd will see the normal compilation for this system

    """
    return [
        compile_command
        for compile_command in compilation_database
        if "native_client" not in compile_command["command"]
    ]


def main():
    parser = argparse.ArgumentParser(description="Post-process the clang compilation database for Chromium")
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
