#!/usr/bin/env python3

import argparse
import csv
import logging
import os
import pathlib
import re
import sys
from typing import List, Tuple

# Insert this script's directory into the path so it can import sibling modules
# TODO - Is this actually necessary?
sys.path.insert(0, pathlib.Path(__file__).parent.resolve())

from common import IncludeChange

Change = Tuple[IncludeChange, int, str, str, int]

GENERATED_FILE_REGEX = re.compile(r"^out/\w+/gen/.*$")
MOJOM_HEADER_REGEX = re.compile(r"^.*.mojom[^.]*.h$")

# This is a list of known filenames to skip when checking for unused
# includes. It's mostly a list of umbrella headers where the includes
# will appear to clangd to be unused, but are meant to be included.
UNUSED_INCLUDE_FILENAME_SKIP_LIST = (
    "base/trace_event/base_tracing.h",
    "mojo/public/cpp/system/core.h",
    # TODO - Keep populating this list
)

# This is a list of known filenames where clangd produces a false
# positive when suggesting as unused includes to remove. Usually these
# are umbrella headers, or headers where clangd thinks the canonical
# location for a symbol is actually in a forward declaration, causing
# it to flag the correct header as unused everywhere, so ignore those.
UNUSED_INCLUDE_IGNORE_LIST = (
    "base/allocator/buildflags.h",
    "base/bind.h",
    "base/callback.h",
    "base/clang_profiling_buildflags.h",
    "base/compiler_specific.h",
    "base/hash/md5.h",
    "base/strings/string_piece.h",
    "base/trace_event/base_tracing.h",
    "build/branding_buildflags.h",
    "build/build_config.h",
    "build/chromecast_buildflags.h",
    "build/chromeos_buildflags.h",
    "chrome/browser/ui/browser.h",
    "chrome/browser/ui/browser_list.h",
    "chrome/common/buildflags.h",
    "components/safe_browsing/buildflags.h",
    "components/signin/public/base/signin_buildflags.h",
    "content/browser/renderer_host/render_frame_host_impl.h",
    "content/browser/web_contents/web_contents_impl.h",
    "extensions/buildflags/buildflags.h",
    "extensions/renderer/extension_frame_helper.h",
    "media/media_buildflags.h",
    "mojo/public/cpp/bindings/pending_receiver.h",
    "mojo/public/cpp/bindings/pending_remote.h",
    "mojo/public/cpp/bindings/receiver.h",
    "mojo/public/cpp/bindings/remote.h",
    "mojo/public/cpp/system/core.h",
    "ppapi/buildflags/buildflags.h",
    "printing/buildflags/buildflags.h",
    "third_party/blink/renderer/platform/graphics/paint/paint_filter.h",
    "ui/aura/window.h",
    "ui/gfx/image/image_skia_rep.h",
    "v8/include/v8.h",
    # TODO - Keep populating this list
)

# This is a list of known filenames where clangd produces a false
# positive when suggesting as includes to add.
ADD_INCLUDE_IGNORE_LIST: Tuple[str, ...] = (
    "absl/base/internal/inline_variable.h",
    "absl/types/internal/optional.h",
    "base/atomicops_internals_portable.h",
    "base/bind_internal.h",
    "base/callback_forward.h",
    "base/callback_internal.h",
    "base/hash/md5_constexpr_internal.h",
    "base/metrics/histogram_macros_internal.h",
    "base/numerics/safe_conversions_impl.h",
    "base/numerics/safe_math_clang_gcc_impl.h",
    "base/numerics/safe_math_shared_impl.h",
    "base/strings/string_piece_forward.h",
    "base/trace_event/trace_event_impl.h",
    "ui/base/metadata/metadata_macros_internal.h",
    "ui/gfx/image/image_skia_rep_default.h",
    "v8-internal.h",
    # TODO - Keep populating this list
)

UNUSED_EDGE_IGNORE_LIST = (
    ("base/atomicops.h", "base/atomicops_internals_portable.h"),
    ("base/hash/md5_constexpr.h", "base/hash/md5_constexpr_internal.h"),
    ("base/memory/aligned_memory.h", "base/bits.h"),
    ("base/numerics/safe_math_shared_impl.h", "base/numerics/safe_math_clang_gcc_impl.h"),
    ("base/trace_event/typed_macros.h", "base/tracing/protos/chrome_track_event.pbzero.h"),
    ("chrome/browser/ui/browser.h", "chrome/browser/ui/signin_view_controller.h"),
    (
        "components/download/public/common/download_item_rename_progress_update.h",
        "components/enterprise/common/download_item_reroute_info.h",
    ),
    ("ipc/ipc_message_macros.h", "base/task/common/task_annotator.h"),
    ("mojo/public/cpp/bindings/lib/serialization.h", "mojo/public/cpp/bindings/array_traits_stl.h"),
    ("mojo/public/cpp/bindings/lib/serialization.h", "mojo/public/cpp/bindings/map_traits_stl.h"),
    (
        "third_party/blink/renderer/core/frame/web_feature.h",
        "third_party/blink/public/mojom/web_feature/web_feature.mojom-blink.h",
    ),
    ("third_party/blink/renderer/core/page/page.h", "third_party/blink/public/mojom/page/page.mojom-blink.h"),
    (
        "third_party/blink/renderer/core/probe/core_probes.h",
        "third_party/blink/renderer/core/core_probes_inl.h",
    ),
    (
        "third_party/blink/renderer/platform/wtf/allocator/allocator.h",
        "base/allocator/partition_allocator/partition_alloc.h",
    ),
    (
        "third_party/blink/renderer/platform/wtf/hash_table.h",
        "third_party/blink/renderer/platform/wtf/hash_iterators.h",
    ),
    (
        "third_party/blink/renderer/platform/wtf/text/atomic_string.h",
        "third_party/blink/renderer/platform/wtf/text/string_concatenate.h",
    ),
    ("services/network/public/cpp/url_request_mojom_traits.h", "services/network/public/cpp/resource_request.h"),
    ("ui/gfx/image/image_skia_rep.h", "ui/gfx/image/image_skia_rep_default.h"),
    # TODO - Keep populating this list
)


def filter_changes(
    changes: List[Change],
    filename_filter: re.Pattern = None,
    header_filter: re.Pattern = None,
    change_type_filter: IncludeChange = None,
    filter_generated_files=True,
    filter_mojom_headers=True,
    filter_false_positives=True,
):
    """Filter changes"""

    for change_type_value, line, filename, header, *_ in changes:
        change_type = IncludeChange.from_value(change_type_value)

        if change_type is None:
            logging.warning(f"Skipping unknown change type: {change_type_value}")
            continue
        elif change_type_filter and change_type != change_type_filter:
            continue

        if filename_filter and not filename_filter.match(filename):
            continue
        elif header_filter and not header_filter.match(header):
            continue

        if filter_generated_files and GENERATED_FILE_REGEX.match(filename):
            continue

        if filter_mojom_headers and MOJOM_HEADER_REGEX.match(header):
            continue

        # Cut down on noise by ignoring known false positives
        if filter_false_positives:
            if change_type is IncludeChange.REMOVE:
                if filename in UNUSED_INCLUDE_FILENAME_SKIP_LIST:
                    logging.info(f"Skipping filename for unused includes: {filename}")
                    continue

                ignore_edge = (filename, header) in UNUSED_EDGE_IGNORE_LIST
                ignore_include = header in UNUSED_INCLUDE_IGNORE_LIST

                # TODO - Ignore unused suggestion if the include is for the associated header

                if ignore_edge or ignore_include:
                    continue
            elif change_type is IncludeChange.ADD:
                if header in ADD_INCLUDE_IGNORE_LIST:
                    continue

        yield (change_type_value, line, filename, header, *_)


def main():
    parser = argparse.ArgumentParser(description="Filter include changes output")
    parser.add_argument(
        "changes_file",
        type=argparse.FileType("r"),
        help="CSV of include changes to filter.",
    )
    parser.add_argument("--filename-filter", help="Regex to filter which files have changes outputted.")
    parser.add_argument("--header-filter", help="Regex to filter which headers are included in the changes.")
    parser.add_argument("--no-filter-generated-files", action="store_true", help="Filter out generated files.")
    parser.add_argument("--no-filter-mojom-headers", action="store_true", help="Filter out mojom headers.")
    parser.add_argument("--no-filter-false-positives", action="store_true", help="Filter out known false positives.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--add-only", action="store_true", default=False, help="Only output includes to add.")
    group.add_argument("--remove-only", action="store_true", default=False, help="Only output includes to remove.")
    parser.add_argument("--verbose", action="store_true", default=False, help="Enable verbose logging.")
    args = parser.parse_args()

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

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    if args.add_only:
        change_type_filter = IncludeChange.ADD
    elif args.remove_only:
        change_type_filter = IncludeChange.REMOVE
    else:
        change_type_filter = None

    csv_writer = csv.writer(sys.stdout)

    try:
        for change in filter_changes(
            csv.reader(args.changes_file),
            filename_filter=filename_filter,
            header_filter=header_filter,
            change_type_filter=change_type_filter,
            filter_generated_files=not args.no_filter_generated_files,
            filter_mojom_headers=not args.no_filter_mojom_headers,
            filter_false_positives=not args.no_filter_false_positives,
        ):
            csv_writer.writerow(change)

        sys.stdout.flush()
    except BrokenPipeError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(1)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass  # Don't show the user anything
