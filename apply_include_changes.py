#!/usr/bin/env python3

import argparse
import asyncio
import csv
import logging
import pathlib
import re
import sys
from collections import defaultdict
from typing import Dict, List, Tuple

from common import IncludeChange

INCLUDE_REGEX = re.compile(r"\s*#include ([\"<](.*)[\">])")

Change = Tuple[IncludeChange, int, str]


# TODO - Refactor this to take filename and list of changes instead of file_changes
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
                    skip = False

                    if header.startswith("<"):
                        include_line = f"#include {header}"
                    else:
                        include_line = f'#include "{header}"'

                    for idx, line in enumerate(lines):
                        if line.strip() == include_line:
                            logging.warning(f"Skipping, include already present: {filename}:{idx + 1}:{include_line}")
                            skip = True
                            break

                    if not skip:
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
        "--src-root", type=pathlib.Path, help="Path to the source tree root.", default=pathlib.Path(".")
    )
    parser.add_argument("--max-count", type=int, help="Maximum number of changes to apply.")
    parser.add_argument(
        "--dry-run", action="store_true", default=False, help="Don't save files to disk, just try to apply them."
    )
    parser.add_argument("--verbose", action="store_true", default=False, help="Enable verbose logging.")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    changes: Dict[str, List[Change]] = defaultdict(list)
    changes_count = 0

    for change_type_value, line, filename, header, *_ in csv.reader(args.changes_file):
        change_type = IncludeChange.from_value(change_type_value)

        if change_type is None:
            logging.warning(f"Skipping unknown change type: {change_type_value}")
            continue

        changes[filename].append((change_type, int(line), header))
        changes_count += 1

        if args.max_count and changes_count == args.max_count:
            break

    apply_changes(args.src_root, changes, save_changes=not args.dry_run)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass  # Don't show the user anything
