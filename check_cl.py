#!/usr/bin/env python3

import argparse
import asyncio
import base64
import csv
import json
import logging
import pathlib
import sys
import urllib.parse
import urllib.request
from typing import Dict, List, Mapping, Tuple

# Insert this script's directory into the path so it can import sibling modules
# TODO - Is this actually necessary?
sys.path.insert(0, pathlib.Path(__file__).parent.resolve())

from clangd_lsp import ClangdClient, ClangdPublishDiagnostics, parse_includes_from_diagnostics

GERRIT_BASE_URL = "https://chromium-review.googlesource.com"
CHANGE_FILES_ENDPOINT = GERRIT_BASE_URL + "/changes/chromium%2Fsrc~{change_list}/revisions/{revision}/files"
GET_CONTENT_ENDPOINT = CHANGE_FILES_ENDPOINT + "/{filename}/content"


# This script is fairly well parallelize with asyncio, except for HTTP requests,
# and file operations on disk. The benefit of using libraries to make those async
# may be minimal, shaving a few seconds per run. We can see from this timing of a
# run on a 4C/8T system that it does decently well on parallelization while running
#
# real	0m20.103s
# user	1m28.616s
# sys	0m4.145s


# TODO - Consider using aiohttp to parallelize this
def download_cl_files(change_list: int) -> Dict[str, bytes]:
    """Downloads all files in a CL and returns a dict of filename to file content"""

    cl_files_response = urllib.request.urlopen(
        CHANGE_FILES_ENDPOINT.format(change_list=change_list, revision="current")
    )
    cl_files_response.readline()  # )]}'
    filenames = set(json.loads(cl_files_response.read()).keys())
    filenames.remove("/COMMIT_MSG")

    files = {}

    for filename in filenames:
        # Skip files which aren't source code
        if not filename.endswith(".h") and not filename.endswith(".cc") and not filename.endswith(".mm"):
            continue

        files[filename] = base64.b64decode(
            urllib.request.urlopen(
                GET_CONTENT_ENDPOINT.format(
                    change_list=change_list, revision="current", filename=urllib.parse.quote(filename, safe="")
                )
            ).read()
        )

    return files


async def check_cl(
    clangd_client: ClangdClient, change_list: int
) -> Mapping[str, Tuple[Tuple[str, ...], Tuple[str, ...]]]:
    """Check a CL and return any include suggestions which have changed as a result of the CL changes"""

    # TODO - Add a backstop in case this is run on a CL with a huge number of files changed
    files = download_cl_files(change_list)

    initial_includes = {}
    cl_includes = {}
    original_file_contents = {}

    # Don't worry about limiting to a certain number of jobs at once, clangd will
    # queue files itself, and since a CL won't have too many files, it's fine to
    # just send everything to clangd and let it be queued there and let it work
    async def get_diagnostics(filename: str, version: int = 1):
        async with clangd_client.listen_for_notifications() as notifications, clangd_client.with_document(
            filename
        ) as document:
            async for notification in notifications:
                if (
                    isinstance(notification, ClangdPublishDiagnostics)
                    and notification.uri == document.uri
                    and notification.version == version
                ):
                    includes = parse_includes_from_diagnostics(filename, document, notification.diagnostics)
                    return filename, document, includes

    # Get the initial includes for the files as they exist on HEAD
    for filename, document, includes in await asyncio.gather(*[get_diagnostics(filename) for filename in files]):
        original_file_contents[filename] = document.text
        initial_includes[filename] = includes

    try:
        # Now change the file content on disk to the CL version
        # TODO - Consider aiofiles, although performance impact is likely negligible
        for filename in files:
            with open((clangd_client.root_path / filename), "w") as f:
                f.write(files[filename].decode("utf8"))

        # Get the include suggestions for the files as they exist in the CL
        for filename, document, includes in await asyncio.gather(*[get_diagnostics(filename) for filename in files]):
            cl_includes[filename] = includes
    finally:
        # Always try to restore the file content
        for filename in original_file_contents:
            with open((clangd_client.root_path / filename), "w") as f:
                f.write(original_file_contents[filename])

    final_includes = {}

    # Compare initial includes to the CL version includes and see what changed
    for filename in files:
        final_add: List[str] = []
        final_remove: List[str] = []

        initial_add, initial_remove = initial_includes[filename]
        cl_add, cl_remove = cl_includes[filename]

        for add in cl_add:
            if add not in initial_add:
                final_add.append(add)

        for remove in cl_remove:
            if remove not in initial_remove:
                final_remove.append(remove)

        final_includes[filename] = (tuple(final_add), tuple(final_remove))

    return final_includes


# TODO - How to detect when compilation DB isn't found and clangd is falling back (won't work)
async def main():
    parser = argparse.ArgumentParser(
        description="Check a CL to see if it should add or remove an include as a result of changes"
    )
    parser.add_argument(
        "change_list",
        type=int,
        help="The CL number to check.",
    )
    parser.add_argument(
        "--clangd-path",
        type=pathlib.Path,
        help="Path to the clangd executable to use.",
        default="clangd.exe" if sys.platform == "win32" else "clangd",
    )
    parser.add_argument(
        "--chromium-src", type=pathlib.Path, help="Path to the Chromium source tree.", default=pathlib.Path(".")
    )
    parser.add_argument(
        "--compile-commands-dir", type=pathlib.Path, help="Specify a path to look for compile_commands.json."
    )
    parser.add_argument("--verbose", action="store_true", default=False, help="Enable verbose logging.")
    args = parser.parse_args()

    if args.compile_commands_dir and not args.compile_commands_dir.is_dir():
        print("error: --compile-commands-dir must be a directory")
        return 1

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    root_path = args.chromium_src.resolve()

    if not ClangdClient.validate_config(root_path):
        print("error: Must have a .clangd config with IncludeCleaner enabled")
        return 3

    async def start_clangd_client():
        clangd_client = ClangdClient(
            args.clangd_path,
            root_path,
            args.compile_commands_dir.resolve() if args.compile_commands_dir else None,
        )
        await clangd_client.start()

        return clangd_client

    csv_writer = csv.writer(sys.stdout)

    clangd_client = await start_clangd_client()

    try:
        # TODO - Consider retry logic on clangd crash
        include_warnings = await check_cl(clangd_client, args.change_list)

        for filename in include_warnings:
            add, remove = include_warnings[filename]

            for include in add:
                csv_writer.writerow(("add", filename, include))

            for include in remove:
                csv_writer.writerow(("remove", filename, include))
    finally:
        await clangd_client.exit()

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass  # Don't show the user anything
