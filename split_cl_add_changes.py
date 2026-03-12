#!/usr/bin/env python3

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError

GERRIT_URL = "https://chromium-review.googlesource.com"

# Matches added include lines in a unified diff, capturing the header path
ADDED_INCLUDE_RE = re.compile(r"^\+\s*#\s*include\s+([<\"])([^>\"]+)[>\"]")

# Matches clang compilation error/warning lines
# See: https://github.com/electron/electron/blob/main/.github/problem-matchers/clang.json
CLANG_ERROR_RE = re.compile(
    r"^(.+)[(:](\d+)[,:](\d+)\)?:\s+(warning|error):\s+(.*)$",
    re.MULTILINE,
)

LOGS_URL_TEMPLATE = (
    "https://logs.chromium.org/logs/chromium/buildbucket/cr-buildbucket/"
    "{build_id}/+/u/compile__with_patch_/"
    "raw_io.output_text_failure_summary_?format=raw"
)

LOGS_URL_TEMPLATE_ALT = (
    "https://logs.chromium.org/logs/chromium/buildbucket/cr-buildbucket/"
    "{build_id}/+/u/compile/"
    "raw_io.output_text_failure_summary_?format=raw"
)


def get_auth_cookies(gitcookies_path="~/.gitcookies"):
    """Get authentication cookie from .gitcookies file.

    Based on _get_auth_for_gitcookies from gerrit-mcp-server.
    """
    path = os.path.expanduser(gitcookies_path)
    if not os.path.exists(path):
        return None

    domain = GERRIT_URL.replace("https://", "").replace("http://", "").split("/")[0]

    last_cookie = None
    with open(path, "r") as f:
        for line in f:
            if domain in line:
                parts = line.strip().split("\t")
                if len(parts) == 7:
                    last_cookie = f"{parts[5]}={parts[6]}"

    return last_cookie


def gerrit_request(url, cookie=None, method="GET", data=None, raw=False):
    """Make an authenticated request to Gerrit API."""
    headers = {}
    if cookie:
        headers["Cookie"] = cookie
    if data is not None:
        headers["Content-Type"] = "application/json; charset=UTF-8"

    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = Request(url, data=body, headers=headers, method=method)

    try:
        with urlopen(req) as response:
            text = response.read().decode("utf-8")
    except HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        logging.error(f"Gerrit API error {e.code}: {error_body}")
        raise

    if raw:
        return text

    # Gerrit prefixes JSON responses with )]}'
    if text.startswith(")]}'"):
        text = text[4:]
    return json.loads(text)


def get_change_info(change_id, cookie=None):
    """Get details of a change including project and branch."""
    url = f"{GERRIT_URL}/a/changes/{quote(str(change_id), safe='')}"
    return gerrit_request(url, cookie=cookie)


def get_patch_raw(change_id, revision="current", cookie=None):
    """Get the raw patch text for a change revision."""
    url = f"{GERRIT_URL}/a/changes/{quote(str(change_id), safe='')}/revisions/{revision}/patch?raw"
    return gerrit_request(url, cookie=cookie, raw=True)


def create_change_with_patch(
    project,
    branch,
    subject,
    patch_text,
    work_in_progress=False,
    cookie=None,
):
    """Create a new change with an initial patch via the Gerrit Create Change API."""
    url = f"{GERRIT_URL}/a/changes/"
    data = {
        "project": project,
        "branch": branch,
        "subject": subject,
        "status": "NEW",
        "patch": {
            "patch": patch_text,
        },
    }
    if work_in_progress:
        data["work_in_progress"] = True

    return gerrit_request(url, cookie=cookie, method="POST", data=data)


def parse_patch_files(patch_text):
    """Parse a unified diff into file-level sections.

    Returns list of (file_header_lines, hunks) where each hunk is
    (hunk_header, hunk_lines).
    """
    files = []
    current_file_header = []
    current_hunks = []
    current_hunk_header = None
    current_hunk_lines = []

    for line in patch_text.split("\n"):
        if line.startswith("diff --git"):
            # Save previous hunk and file
            if current_hunk_header is not None:
                current_hunks.append((current_hunk_header, current_hunk_lines))
            if current_file_header:
                files.append((current_file_header, current_hunks))
            current_file_header = [line]
            current_hunks = []
            current_hunk_header = None
            current_hunk_lines = []
        elif line.startswith("@@"):
            # Save previous hunk
            if current_hunk_header is not None:
                current_hunks.append((current_hunk_header, current_hunk_lines))
            current_hunk_header = line
            current_hunk_lines = []
        elif current_hunk_header is not None:
            current_hunk_lines.append(line)
        else:
            # Part of file header (index, ---, +++ lines)
            current_file_header.append(line)

    # Save last hunk and file
    if current_hunk_header is not None:
        current_hunks.append((current_hunk_header, current_hunk_lines))
    if current_file_header:
        files.append((current_file_header, current_hunks))

    return files


def find_added_includes(patch_text):
    """Find all added #include lines in the patch.

    Returns a dict mapping header_name -> list of file paths where it's added.
    """
    includes = defaultdict(list)
    current_file = None

    for line in patch_text.split("\n"):
        if line.startswith("diff --git"):
            match = re.match(r"^diff --git a/.+ b/(.+)$", line)
            if match:
                current_file = match.group(1)
        if current_file:
            m = ADDED_INCLUDE_RE.match(line)
            if m:
                header = m.group(2)
                if current_file not in includes[header]:
                    includes[header].append(current_file)

    return dict(includes)


def filter_patch_for_header(patch_text, target_header):
    """Create a new patch that only contains #include additions for target_header.

    Other added lines are removed. Removed lines from the original patch are
    converted to context lines (they exist in the base file and should remain
    as context in the new patch).
    """
    files = parse_patch_files(patch_text)
    result_lines = []

    for file_header, hunks in files:
        filtered_hunks = []

        for hunk_header, hunk_lines in hunks:
            new_lines = []
            has_target_addition = False

            for line in hunk_lines:
                if line.startswith("+"):
                    m = ADDED_INCLUDE_RE.match(line)
                    if m and m.group(2) == target_header:
                        new_lines.append(line)
                        has_target_addition = True
                    # Skip other added lines (they don't exist in the base)
                elif line.startswith("-"):
                    # Convert removals to context (line exists in the base file)
                    new_lines.append(" " + line[1:])
                else:
                    # Context line
                    new_lines.append(line)

            if has_target_addition:
                # Strip trailing empty context lines that might cause issues
                while new_lines and new_lines[-1] == "":
                    new_lines.pop()

                # Recalculate hunk header
                hunk_match = re.match(
                    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$",
                    hunk_header,
                )
                if hunk_match:
                    old_start = int(hunk_match.group(1))
                    old_count = sum(1 for l in new_lines if not l.startswith("+"))
                    new_count = len(new_lines)
                    suffix = hunk_match.group(5) or ""

                    new_hunk_header = f"@@ -{old_start},{old_count}" f" +{old_start},{new_count} @@{suffix}"
                    filtered_hunks.append((new_hunk_header, new_lines))

        if filtered_hunks:
            result_lines.extend(file_header)
            for hunk_header, hunk_lines in filtered_hunks:
                result_lines.append(hunk_header)
                result_lines.extend(hunk_lines)

    return "\n".join(result_lines)


def get_change_revisions(change_id, cookie=None):
    """Get all patchset numbers for a change."""
    url = f"{GERRIT_URL}/a/changes/{quote(str(change_id), safe='')}" f"?o=ALL_REVISIONS"
    data = gerrit_request(url, cookie=cookie)
    revisions = data.get("revisions", {})
    return sorted(rev["_number"] for rev in revisions.values())


def get_try_results_for_patchset(change_id, patchset, cwd=None):
    """Call ``git cl try-results`` and return parsed JSON."""
    fd, tmp_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        result = subprocess.run(
            [
                "git",
                "cl",
                "try-results",
                "-i",
                str(change_id),
                "-p",
                str(patchset),
                f"--json={tmp_path}",
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        if result.returncode != 0:
            logging.warning(
                "git cl try-results failed for patchset %s: %s",
                patchset,
                result.stderr.strip(),
            )
            return []
        with open(tmp_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        logging.warning(
            "Could not parse try results for patchset %s: %s",
            patchset,
            e,
        )
        return []
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def get_failed_build_ids(try_results):
    """Extract build IDs from failed try results.

    When a build has a ``parent_buildbucket_id`` tag it is a child
    (compilator) build whose logs contain the actual errors.  In that
    case we use the child ID and drop the parent from the result set.
    """
    failed_ids = []
    parent_ids_to_remove = set()
    for result in try_results:
        status = result.get("status")
        build_id = result.get("id")
        if status == "FAILURE" and build_id:
            parent_id = None
            for tag in result.get("tags", []):
                if tag.get("key") == "parent_buildbucket_id":
                    parent_id = tag.get("value")
                    break
            if parent_id:
                parent_ids_to_remove.add(parent_id)
            failed_ids.append(str(build_id))
    return [bid for bid in failed_ids if bid not in parent_ids_to_remove]


def fetch_error_log(build_id, cookie=None):
    """Fetch the raw compilation error log for a failed build."""
    headers = {}
    if cookie:
        headers["Cookie"] = cookie

    urls = [
        LOGS_URL_TEMPLATE.format(build_id=build_id),
        LOGS_URL_TEMPLATE_ALT.format(build_id=build_id),
    ]
    for url in urls:
        try:
            req = Request(url, headers=headers)
            with urlopen(req) as response:
                return response.read().decode("utf-8")
        except HTTPError as e:
            if e.code == 404 and url is not urls[-1]:
                logging.debug(
                    "Got 404 for build %s, trying alternate URL...",
                    build_id,
                )
                continue
            logging.warning("Could not fetch error log for build %s: %s", build_id, e)
            return ""


def normalize_error_path(filepath):
    """Normalize a compiler-output path to be relative to the repo root."""
    # Strip ../../ prefix (common in Chromium out/ relative paths)
    while filepath.startswith("../"):
        filepath = filepath[3:]
    # Handle absolute paths by extracting after /src/
    if "/src/" in filepath:
        filepath = filepath.split("/src/", 1)[1]
    return filepath


def _parse_caret_line(line):
    """Parse a clang caret/tilde line and return 0-based (start, end) or None.

    The returned *start* is the 0-based column of the ``^`` character and
    *end* is the 0-based exclusive end (one past the last ``~``, or one past
    the ``^`` when there are no tildes).
    """
    # Strip the line-number + pipe prefix used by modern clang, e.g.
    #   "      |   ^~~~"
    if "|" in line:
        content = line.split("|", 1)[1]
        if content.startswith(" "):
            content = content[1:]
    else:
        content = line

    # Ensure the content is *only* a caret/tilde indicator
    if not re.match(r"^\s*\^~*\s*$", content):
        return None

    match = re.search(r"\^~*", content)
    if not match:
        return None

    return (match.start(), match.end())


def parse_compilation_errors(log_text, build_id=None):
    """Parse clang compilation errors from log text.

    Returns a dict mapping normalised filepath to a set of
    ``(line, message, build_id, range_tuple)`` tuples where *range_tuple*
    is either ``None`` or ``(start_line, start_character, end_line,
    end_character)`` derived from the clang caret/tilde diagnostic output.
    """
    errors = defaultdict(set)
    lines = log_text.splitlines()
    for i, line_text in enumerate(lines):
        match = CLANG_ERROR_RE.match(line_text)
        if not match:
            continue
        filepath = match.group(1)
        line = int(match.group(2))
        severity = match.group(4)
        message = match.group(5)
        if severity == "error":
            normalized = normalize_error_path(filepath)
            # Look ahead two lines for a caret/tilde range indicator
            range_tuple = None
            if i + 2 < len(lines):
                caret_range = _parse_caret_line(lines[i + 2])
                if caret_range is not None:
                    start_char, end_char = caret_range
                    range_tuple = (line, start_char, line, end_char)
            errors[normalized].add((line, message, build_id, range_tuple))
    return errors


def create_gerrit_draft(change_id, revision, filepath, line, message, cookie=None, comment_range=None):
    """Create a draft comment on a Gerrit change."""
    url = f"{GERRIT_URL}/a/changes/{quote(str(change_id), safe='')}" f"/revisions/{revision}/drafts"
    data = {
        "path": filepath,
        "side": "PARENT",
        "message": message,
    }
    if comment_range is not None:
        data["range"] = comment_range
    else:
        data["line"] = line
    return gerrit_request(url, cookie=cookie, method="PUT", data=data)


def collect_compilation_errors(change_id, cookie=None, chromium_src=None):
    """Collect compilation errors from failed try results on a CL.

    Returns a dict mapping normalised filepath to a set of
    ``(line, message, build_id)`` tuples, or ``None`` if no errors were found.
    """
    revisions = get_change_revisions(change_id, cookie=cookie)
    if not revisions:
        print("No revisions found for the change")
        return None

    print(f"Found {len(revisions)} revision(s)")

    # Collect failed build IDs across all revisions
    all_failed_ids = []
    for patchset in revisions:
        print(f"Getting try results for patchset {patchset}...")
        try_results = get_try_results_for_patchset(change_id, patchset, cwd=chromium_src)
        failed = get_failed_build_ids(try_results)
        if failed:
            print(f"  Found {len(failed)} failed build(s)")
            all_failed_ids.extend(failed)

    if not all_failed_ids:
        print("No failed builds found across any revision")
        return None

    # Deduplicate while preserving order
    all_failed_ids = list(dict.fromkeys(all_failed_ids))
    print(f"\nTotal unique failed builds: {len(all_failed_ids)}")

    # Fetch and parse error logs
    # filepath -> dict of (line, message) -> (build_id, range_tuple) (first seen)
    all_errors = defaultdict(dict)
    for build_id in all_failed_ids:
        print(f"Fetching error log for build {build_id}...")
        log_text = fetch_error_log(build_id)
        if not log_text:
            continue
        errors = parse_compilation_errors(log_text, build_id=build_id)
        for filepath, error_set in errors.items():
            for line, message, bid, range_tuple in error_set:
                key = (line, message)
                if key not in all_errors[filepath]:
                    all_errors[filepath][key] = (bid, range_tuple)

    if not all_errors:
        print("No compilation errors found in any error log")
        return None

    # Convert to set of (line, message, build_id, range_tuple) tuples
    result = {}
    for filepath, error_dict in all_errors.items():
        result[filepath] = {
            (line, message, bid, range_tuple) for (line, message), (bid, range_tuple) in error_dict.items()
        }

    total_files = len(result)
    total_errors = sum(len(errs) for errs in result.values())
    print(f"\nFound {total_errors} unique error(s) across {total_files} file(s)")
    return result


def annotate_split_cl(change_id, files_in_cl, all_errors, cookie=None, dry_run=False):
    """Annotate a newly-created split CL with relevant compilation errors.

    *files_in_cl* is the list of source files touched by this CL.
    *all_errors* is the full error mapping from :func:`collect_compilation_errors`.

    A single comment is placed per file at the earliest error line,
    concatenating all errors for that file.

    Returns the number of draft comments created (or that would be created
    in dry-run mode).
    """
    comments_created = 0

    for filepath in sorted(files_in_cl):
        error_set = all_errors.get(filepath)
        if not error_set:
            if dry_run:
                print(f"    (no errors) {filepath}")
            continue

        # Place the comment at the earliest error line, but include all errors
        earliest_line = min(line for line, _, _, _ in error_set)
        all_messages = sorted((line, msg, build_id, range_tuple) for line, msg, build_id, range_tuple in error_set)
        comment_parts = []
        for line, msg, bid, _ in all_messages:
            if line != earliest_line:
                part = f"Line {line}: `{msg}`"
            else:
                part = f"`{msg}`"
            if bid:
                part += f"\n\nhttps://ci.chromium.org/b/{bid}"
            comment_parts.append(part)
        comment = "\n\n".join(comment_parts)

        # Build Gerrit CommentRange from the first available caret range
        # at the earliest error line
        comment_range = None
        for line, _, _, rt in all_messages:
            if line == earliest_line and rt is not None:
                comment_range = {
                    "start_line": rt[0],
                    "start_character": rt[1],
                    "end_line": rt[2],
                    "end_character": rt[3],
                }
                break

        if dry_run:
            if comment_range:
                print(
                    f"    comment {filepath}:{earliest_line}:{comment_range['start_character']}-{comment_range['end_character']}"
                )
            else:
                print(f"    comment {filepath}:{earliest_line}")
            for line, msg, bid, _ in all_messages:
                if line != earliest_line:
                    print(f"      Line {line}: {msg}")
                else:
                    print(f"      {msg}")
                if bid:
                    print(f"      https://ci.chromium.org/b/{bid}")
            comments_created += 1
            continue

        try:
            create_gerrit_draft(
                change_id,
                "current",
                filepath,
                earliest_line,
                comment,
                cookie=cookie,
                comment_range=comment_range,
            )
            comments_created += 1
        except HTTPError as e:
            print(f"error: Failed to create draft on {filepath}:{earliest_line}: {e}")

    return comments_created


def main():
    parser = argparse.ArgumentParser(
        description="Split the added #include lines in a CL into separate CLs, " "one per unique header."
    )
    parser.add_argument(
        "change_id",
        type=str,
        help="The Gerrit change ID or number of the CL to split",
    )
    parser.add_argument(
        "-n",
        "--min-includes",
        type=int,
        default=1,
        help="Only split off a CL for a header if it has at least this many " "added includes (default: 1, split all)",
    )
    parser.add_argument(
        "--gitcookies-path",
        type=str,
        default="~/.gitcookies",
        help="Path to the .gitcookies file for authentication " "(default: ~/.gitcookies)",
    )
    parser.add_argument(
        "--wip",
        action="store_true",
        default=False,
        help="Mark the created CLs as work-in-progress",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print what would be done without creating CLs",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--annotate-errors",
        action="store_true",
        default=False,
        help="Annotate each new split CL with draft comments showing the "
        "compilation errors (from the original CL's try results) that "
        "demonstrate why each added include is needed",
    )
    parser.add_argument(
        "--chromium-src",
        type=str,
        default=None,
        help="Path to a Chromium src checkout (required for --annotate-errors, "
        "as git cl try-results must be run from a Chromium src directory)",
    )
    args = parser.parse_args()

    if args.annotate_errors and not args.chromium_src:
        parser.error("--chromium-src is required when using --annotate-errors")

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    # Authenticate
    cookie = get_auth_cookies(args.gitcookies_path)
    if not cookie:
        print("error: Could not find authentication cookie in gitcookies file " f"for {GERRIT_URL}")
        print("hint: Make sure your .gitcookies file exists and contains an " "entry for the Gerrit instance")
        return 1

    # Collect compilation errors from the original CL if annotation requested
    all_errors = None
    if args.annotate_errors:
        print("Collecting compilation errors from original CL try results...\n")
        all_errors = collect_compilation_errors(
            args.change_id,
            cookie=cookie,
            chromium_src=args.chromium_src,
        )
        if all_errors:
            print()  # blank line before the split output
        else:
            print("warning: No compilation errors found; " "split CLs will not be annotated\n")

    # Get change details
    try:
        change_info = get_change_info(args.change_id, cookie=cookie)
    except HTTPError as e:
        print(f"error: Could not fetch change {args.change_id}: {e}")
        return 1

    project = change_info["project"]
    branch = change_info["branch"]
    change_number = change_info["_number"]
    subject = change_info.get("subject", "")
    logging.debug(f"Change {change_number}: project={project}, branch={branch}, " f"subject={subject}")

    # Get the patch
    try:
        patch_text = get_patch_raw(args.change_id, cookie=cookie)
    except HTTPError as e:
        print(f"error: Could not fetch patch for change {args.change_id}: {e}")
        return 1

    # Find added includes
    added_includes = find_added_includes(patch_text)
    num_headers = len(added_includes)
    total_additions = sum(len(files) for files in added_includes.values())

    if num_headers == 0:
        print("No added #include lines found in the patch")
        return 0

    print(f"Found {total_additions} added #include line(s) for " f"{num_headers} unique header(s)")

    # Filter to only headers meeting the minimum threshold
    eligible = {h: f for h, f in added_includes.items() if len(f) >= args.min_includes}

    if not eligible:
        print(f"No headers have at least {args.min_includes} added include(s), " "nothing to split")
        return 0

    if args.dry_run:
        print(f"\nDry run - would create {len(eligible)} CL(s):\n")

    created_cls = []
    total_annotations = 0

    for header, files in sorted(eligible.items()):
        new_subject = f"Add some missing includes of {header}\n\nBug: 40216326"

        if args.dry_run:
            print(f"  {new_subject}")
            for f in files:
                print(f"    - {f}")
            if all_errors:
                n = annotate_split_cl(
                    None,
                    files,
                    all_errors,
                    dry_run=True,
                )
                total_annotations += n
            continue

        # Generate the filtered patch for this header
        split_patch = filter_patch_for_header(patch_text, header)
        if not split_patch.strip():
            logging.warning(f"Empty patch generated for header {header}, skipping")
            continue

        try:
            result = create_change_with_patch(
                project,
                branch,
                new_subject,
                split_patch,
                work_in_progress=args.wip,
                cookie=cookie,
            )
            cl_number = result["_number"]
            created_cls.append(cl_number)
            print(f"Created CL {cl_number}: {new_subject} " f"({GERRIT_URL}/c/{cl_number})")
        except HTTPError as e:
            print(f"error: Failed to create CL for header '{header}': {e}")
            logging.debug(f"Patch content:\n{split_patch[:500]}")
            continue

        # Annotate the newly-created CL with relevant compilation errors
        if all_errors:
            n = annotate_split_cl(
                cl_number,
                files,
                all_errors,
                cookie=cookie,
            )
            if n:
                print(f"  Added {n} draft comment(s) with compilation errors")
            total_annotations += n

    if created_cls:
        print(f"\nCreated {len(created_cls)} CL(s)")
    if total_annotations:
        print(f"Added {total_annotations} total draft comment(s) across split CLs")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
