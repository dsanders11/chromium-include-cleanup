#!/usr/bin/env python3

import argparse
import asyncio
import csv
import enum
import logging
import pathlib
import re
import sys
from collections import defaultdict
from typing import Dict, List, Tuple

# Insert this script's directory into the path so it can import sibling modules
# TODO - Is this actually necessary?
sys.path.insert(0, pathlib.Path(__file__).parent.resolve())


INCLUDE_REGEX = re.compile(r"\s*#include ([\"<](.*)[\">])")


class IncludeChange(enum.Enum):
    ADD = "add"
    REMOVE = "remove"

    @classmethod
    def from_value(cls, value):
        for enum_value in cls:
            if enum_value.value == value:
                return enum_value


Change = Tuple[IncludeChange, int, str]


def apply_changes(root_path: pathlib.Path, file_changes: Dict[str, List[Change]], save_changes=True):
    """Apply changes to files"""

    if not save_changes:
        logging.debug("Not saving changes to files")

    for filename, changes in file_changes.items():
        # Sort the changes by line number so they can be applied in order
        changes = sorted(changes, key=lambda x: x[1])
        line_offset = 0

        with open(filename, "r+") as f:
            lines = f.readlines()

            # Apply the changes and track the line offset as changes are applied
            for change_type, line_number, header in changes:
                current_line_number = line_number + line_offset

                if change_type is IncludeChange.REMOVE:
                    # Confirm that the line looks as expected before removing
                    current_line = lines[current_line_number].strip()
                    include_match = INCLUDE_REGEX.match(current_line)

                    if include_match is None:
                        logging.warning(
                            f"Skipping removing line {filename}:{current_line_number}, line doesn't match an include: {current_line}"
                        )
                        continue

                    include = include_match.group(1).strip('"')

                    if include != header:
                        logging.warning(
                            f"Skipping removing {filename}:{current_line_number}, expected: '{header}', found '{include}'"
                        )
                        continue

                    logging.debug(f"Removed include: {filename}:{current_line_number}:{current_line}")
                    del lines[current_line_number]
                    line_offset -= 1
                elif change_type is IncludeChange.ADD:
                    if header.startswith("<"):
                        include_line = f"#include {header}"
                    else:
                        include_line = f'#include "{header}"'

                    logging.debug(f"Added include: {filename}:{current_line_number}:{include_line}")
                    lines.insert(current_line_number, f"{include_line}\n")
                    line_offset += 1

            # Write the content back out to file with modified includes
            if save_changes:
                f.seek(0)
                f.truncate()
                f.writelines(lines)


async def main():
    parser = argparse.ArgumentParser(description="Apply include changes to files in the source tree")
    parser.add_argument(
        "changes_file",
        type=argparse.FileType("r"),
        help="CSV of changes to apply.",
    )
    parser.add_argument(
        "--chromium-src", type=pathlib.Path, help="Path to the Chromium source tree.", default=pathlib.Path(".")
    )
    parser.add_argument("--filename-filter", help="Regex to filter which files have changes applied.")
    parser.add_argument("--header-filter", help="Regex to filter which headers are included in the changes.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--add-only", action="store_true", default=False, help="Only apply changes which add includes.")
    group.add_argument(
        "--remove-only", action="store_true", default=False, help="Only apply changes which remove includes."
    )
    parser.add_argument("--max-count", type=int, help="Maximum number of changes to apply.")
    parser.add_argument(
        "--dry-run", action="store_true", default=False, help="Don't save files to disk, just try to apply them."
    )
    parser.add_argument("--verbose", action="store_true", default=False, help="Enable verbose logging.")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    try:
        filename_filter = re.compile(args.filename_filter) if args.filename_filter else None
    except Exception:
        print("error: --filename-filter is not a valid regex")
        return 1

    try:
        header_filter = re.compile(args.header_filter) if args.header_filter else None
    except Exception:
        print("error: --header-filter is not a valid regex")
        return 1

    changes: Dict[str, List[Change]] = defaultdict(list)
    changes_count = 0

    for change_type_value, line, filename, header, *_ in csv.reader(args.changes_file):
        change_type = IncludeChange.from_value(change_type_value)

        if change_type is None:
            logging.warning(f"Skipping unknown change type: {change_type_value}")
            continue

        if args.add_only and change_type is not IncludeChange.ADD:
            continue
        elif args.remove_only and change_type is not IncludeChange.REMOVE:
            continue
        elif filename_filter and not filename_filter.match(filename):
            continue
        elif header_filter and not header_filter.match(header):
            continue

        changes[filename].append((change_type, int(line), header))
        changes_count += 1

        if args.max_count and changes_count == args.max_count:
            break

    apply_changes(args.chromium_src, changes, save_changes=not args.dry_run)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass  # Don't show the user anything
