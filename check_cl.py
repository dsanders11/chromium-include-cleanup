#!/usr/bin/env python3

import argparse
import asyncio
import csv
import enum
import io
import json
import logging
import pathlib
import sys
import urllib.parse
import urllib.request
import zipfile
from typing import Dict, Tuple

# Insert this script's directory into the path so it can import sibling modules
# TODO - Is this actually necessary?
sys.path.insert(0, pathlib.Path(__file__).parent.resolve())

from clangd_lsp import ClangdClient, ClangdPublishDiagnostics, parse_includes_from_diagnostics

GERRIT_BASE_URL = "https://chromium-review.googlesource.com"
CODE_OWNERS_URL = GERRIT_BASE_URL + "/changes/{change_list}/code_owners.status?limit=100"
CHANGE_LIST_FILES_URL = GERRIT_BASE_URL + "/changes/chromium%2Fsrc~{change_list}/revisions/{revision}/files"
FILE_ZIP_URL = CHANGE_LIST_FILES_URL + "/{filename}/download"


class IncludeChange(enum.Enum):
    ADD = "add"
    REMOVE = "remove"


def download_and_extract_zip(url: str) -> bytes:
    content = urllib.request.urlopen(url)
    zip_file = zipfile.ZipFile(io.BytesIO(content.read()), "r")

    return zip_file.read(zip_file.namelist()[0])


def download_cl_files(change_list: int) -> Dict[str, str]:
    """Downloads all files in a CL and returns a dict of filename to file content"""

    code_owners_content = urllib.request.urlopen(CODE_OWNERS_URL.format(change_list=change_list))
    code_owners_content.readline()  # )]}\n
    patch_set_number = json.loads(code_owners_content.read())["patch_set_number"]

    cl_files_content = urllib.request.urlopen(
        CHANGE_LIST_FILES_URL.format(change_list=change_list, revision=patch_set_number)
    )
    cl_files_content.readline()  # )]}\n
    filenames = set(json.loads(cl_files_content.read()).keys())
    filenames.remove("/COMMIT_MSG")

    files = {}

    for filename in filenames:
        files[filename] = download_and_extract_zip(
            FILE_ZIP_URL.format(
                change_list=change_list, revision=patch_set_number, filename=urllib.parse.quote(filename, safe="")
            )
        )

    return files


async def check_cl(clangd_client: ClangdClient, change_list: int) -> Dict[str, Tuple[IncludeChange, str, str]]:
    """Check a CL and return any include suggestions which have changed as a result of the CL changes"""

    files = download_cl_files(change_list)

    initial_includes = {}
    cl_includes = {}
    documents = {}
    original_file_contents = {}

    # Get the initial includes for the files as they exist on disk
    for filename in files:
        async with clangd_client.listen_for_notifications() as notifications:
            document = clangd_client.open_document(filename)
            documents[filename] = document
            original_file_contents[filename] = document.text

            async for notification in notifications:
                if (
                    isinstance(notification, ClangdPublishDiagnostics)
                    and notification.uri == document.uri
                    and notification.version == 1
                ):
                    initial_includes[filename] = parse_includes_from_diagnostics(
                        filename, document, notification.diagnostics
                    )
                    break

    try:
        # Now change the file content to the CL version and save the files
        for filename in files:
            text = files[filename].decode("utf8")

            with open((clangd_client.root_path / filename), "w") as f:
                f.write(text)

            clangd_client.save_document(filename)

            # Update the document text as well
            documents[filename].text = text

        # Get the new include suggestions
        for filename in files:
            document = documents[filename]

            async with clangd_client.listen_for_notifications() as notifications:
                # Don't actually change the file content, just use this to bump the version number and force diagnostics
                clangd_client.change_document(filename, 3, documents[filename].text, want_diagnostics=True)

                async for notification in notifications:
                    if (
                        isinstance(notification, ClangdPublishDiagnostics)
                        and notification.uri == document.uri
                        and notification.version == 3
                    ):
                        cl_includes[filename] = parse_includes_from_diagnostics(
                            filename, document, notification.diagnostics
                        )
                        break

        # Clean up documents
        for filename in files:
            if filename in documents:
                clangd_client.close_document(filename)

        final_includes = {}

        # Compare initial includes to the CL version includes and see what changed
        for filename in files:
            final_add = []
            final_remove = []

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
    finally:
        # Always try to restore the file content
        for filename in original_file_contents:
            with open((clangd_client.root_path / filename), "w") as f:
                f.write(original_file_contents[filename])


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