#!/usr/bin/env python3

import argparse
import csv
import logging
import os
import re
import sys
import urllib.request
from datetime import datetime

from extract_archived_include_analysis import extract_include_analysis
from include_analysis import IncludeAnalysisOutput, ParseError, parse_raw_include_analysis_output
from suggest_include_changes import filter_filenames

CHROMIUM_INCLUDE_ANALYSIS_BASE_URL = "https://commondatastorage.googleapis.com/chromium-browser-clang"
HREF_REGEX = re.compile(r"<a href=\"(.*?)\">", re.DOTALL)
FILENAME_DATE_REGEX = re.compile(r"chrome_includes_(\d+-\d+-\d+_\d+)")


class IncludeAnalysisOutputWithUrl(IncludeAnalysisOutput):
    url: str


def extract_include_analysis_list() -> list[str]:
    include_analysis_archive_response = urllib.request.urlopen(
        f"{CHROMIUM_INCLUDE_ANALYSIS_BASE_URL}/chrome_includes-index.html"
    )
    archive_html = include_analysis_archive_response.read().decode("utf8")

    return list(
        map(
            lambda relative_url: f"{CHROMIUM_INCLUDE_ANALYSIS_BASE_URL}/{relative_url}",
            re.findall(HREF_REGEX, archive_html),
        )
    )


def get_archived_include_analysis(analysis_url: str) -> IncludeAnalysisOutputWithUrl:
    include_analysis_response = urllib.request.urlopen(analysis_url)
    include_analysis_contents = include_analysis_response.read().decode("utf8")

    include_analysis_json = extract_include_analysis(include_analysis_contents)

    if not include_analysis_json:
        raise RuntimeError(f"Could not extract include analysis from {analysis_url}")

    # The URL is not included in the JSON, so we add it here so it can be in the output
    include_analysis = parse_raw_include_analysis_output(include_analysis_json)
    include_analysis["url"] = analysis_url

    return include_analysis


def parse_include_analysis_date(analysis_date: str) -> datetime:
    if analysis_date.endswith(" UTC"):
        analysis_date = analysis_date[:-4]

    return datetime.fromisoformat(analysis_date)


def include_analysis_diff(
    include_analysis: IncludeAnalysisOutput,
    min_edge_size: int,
    increase_percentage_threshold: int,
    increase_from_zero_threshold: int,
):
    analysis_date = parse_include_analysis_date(include_analysis["date"])

    flagged_edges = set()

    analysis_list = extract_include_analysis_list()
    analysis_filename_prefix = f"{CHROMIUM_INCLUDE_ANALYSIS_BASE_URL}/chrome_includes_{analysis_date.year}-{analysis_date.month:02d}-{analysis_date.day:02d}"

    # Find index of the provided analysis in case it is not the most recent
    analysis_idx = -1

    # Unfortunately the embedded date is not the same as the filename date,
    # they appear to differ by some amount of seconds, but the filename
    # always has the later timestamp, and the analysis runs are several
    # hours apart, so only check the prefix for the correct hour and the
    # next one as well to account for rollover into the next hour
    for idx, url in enumerate(analysis_list):
        if url.startswith(f"{analysis_filename_prefix}_{analysis_date.hour:02d}") or url.startswith(
            f"{analysis_filename_prefix}_{(analysis_date.hour + 1):02d}"
        ):
            analysis_idx = idx
            break

    if analysis_idx == -1:
        raise RuntimeError("Could not find the analysis in the archive list")

    # Gather previous analyses to compare to if they exist:
    #  * Immediately previous analysis
    #  * At least one week previous
    #  * At least 30 days previous
    previous_analyses = {}

    # First get the immediately previous analysis
    immediately_previous_analysis = get_archived_include_analysis(analysis_list[analysis_idx + 1])
    previous_analysis_date = parse_include_analysis_date(immediately_previous_analysis["date"])
    delta = analysis_date - previous_analysis_date
    previous_analyses[delta.days] = immediately_previous_analysis

    # Look for previous week and previous 30 days
    for min_days_delta in (7, 30):
        for previous_analysis_url in analysis_list:
            match = FILENAME_DATE_REGEX.search(previous_analysis_url)
            if match is None:
                raise RuntimeError(f"Could not parse date from URL: {previous_analysis_url}")

            # Determine the analysis date from the filename
            previous_analysis_date = datetime.strptime(match.group(1).strip(), "%Y-%m-%d_%H%M%S")
            delta = analysis_date - previous_analysis_date

            if delta.days >= min_days_delta:
                # This has already been covered, e.g, previous analysis was already that many days ago
                if delta.days in previous_analyses:
                    break

                previous_analyses[delta.days] = get_archived_include_analysis(previous_analysis_url)
                break

    # Filter out anything that isn't direct Chromium code
    filenames = filter_filenames(
        include_analysis["files"],
        filter_generated_files=True,
        filter_mojom_headers=True,
        filter_third_party=True,
    )

    for previous_analysis in previous_analyses.values():
        for filename in filenames:
            try:
                previous_size = previous_analysis["asizes"][filename]
            except KeyError:
                # New file
                previous_size = 0

            current_size = include_analysis["asizes"][filename]
            difference = current_size - previous_size
            flag_node = False

            # Flag the file itself, not just an edge, if it has a significant increase
            if previous_size == 0:
                flag_node = difference >= increase_from_zero_threshold
            elif current_size > min_edge_size:
                increase_percentage = difference / float(previous_size)
                flag_node = increase_percentage >= increase_percentage_threshold / 100.0

            if flag_node:
                yield (
                    previous_analysis["url"],
                    previous_analysis["revision"],
                    previous_analysis["date"],
                    filename,
                    "",
                    str(difference),
                )

            for header in include_analysis["esizes"][filename]:
                # Only consider the most recent increase if it was flagged
                if (filename, header) in flagged_edges:
                    continue

                try:
                    previous_size = previous_analysis["esizes"][filename][header]
                except KeyError:
                    # New edge
                    previous_size = 0

                current_size = include_analysis["esizes"][filename][header]

                # To cut down on noise, skip edges which are too small to care about
                if current_size < min_edge_size:
                    continue

                difference = current_size - previous_size

                # A lot of edges are zero so a percentage increase isn't applicable,
                # and instead we use an absolute increase in size - otherwise percentage
                if previous_size == 0:
                    flag_edge = difference >= increase_from_zero_threshold
                else:
                    increase_percentage = difference / float(previous_size)
                    flag_edge = increase_percentage >= increase_percentage_threshold / 100.0

                if flag_edge:
                    flagged_edges.add((filename, header))
                    yield (
                        previous_analysis["url"],
                        previous_analysis["revision"],
                        previous_analysis["date"],
                        filename,
                        header,
                        str(difference),
                    )


def main():
    parser = argparse.ArgumentParser(
        description="Analyze differences between an include analysis output and previous ones"
    )
    parser.add_argument(
        "include_analysis_output",
        type=argparse.FileType("r"),
        nargs="?",
        help="The include analysis output to use.",
    )
    parser.add_argument(
        "--min-edge-size",
        type=int,
        help="Minimum edge size in MB before flagging any increase.",
        default=75,
    )
    parser.add_argument(
        "--increase-percentage-threshold",
        type=int,
        help="Increase percentage threshold before flagging increase. 0-100.",
        default=50,
    )
    parser.add_argument(
        "--increase-from-zero-threshold",
        type=int,
        help="Increase in MB threshold before flagging an increase from a previously zero-sized edge.",
        default=75,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--quiet", action="store_true", default=False, help="Only log warnings and errors.")
    group.add_argument("--verbose", action="store_true", default=False, help="Enable verbose logging.")
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(message)s",
        level=logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO,
    )

    # If the user specified an include analysis output file, use that instead of fetching it
    if args.include_analysis_output:
        raw_include_analysis = args.include_analysis_output.read()
    else:
        include_analysis_response = urllib.request.urlopen(
            "https://commondatastorage.googleapis.com/chromium-browser-clang/include-analysis.js"
        )
        raw_include_analysis = include_analysis_response.read().decode("utf8")

    try:
        include_analysis = parse_raw_include_analysis_output(raw_include_analysis)
    except ParseError as e:
        message = str(e)
        print("error: Could not parse include analysis output file")
        if message:
            print(message)
        return 2

    csv_writer = csv.writer(sys.stdout)

    try:
        for row in include_analysis_diff(
            include_analysis,
            args.min_edge_size * 1024 * 1024,
            args.increase_percentage_threshold,
            args.increase_from_zero_threshold * 1024 * 1024,
        ):
            csv_writer.writerow(row)

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
