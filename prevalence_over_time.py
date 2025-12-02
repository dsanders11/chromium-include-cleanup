#!/usr/bin/env python3

import argparse
import csv
import logging
import os
import re
import sys
from datetime import datetime

from suggest_include_changes import filter_filenames
from include_analysis_diff import extract_include_analysis_list, get_archived_include_analysis

FILENAME_FILTER_REGEX = re.compile(r".*.h$")


def get_prevalence_over_time(analysis_list, buckets, latest_per_date=True):
    dates = set()

    for analysis_url in analysis_list:
        include_analysis = get_archived_include_analysis(analysis_url)
        root_count = len(include_analysis["roots"])

        if latest_per_date:
            parsed_date = datetime.strptime(include_analysis["date"].split(" ")[0], "%Y-%m-%d").date()

            if parsed_date in dates:
                continue

            dates.add(parsed_date)

        # Filter out anything that isn't direct Chromium code
        filenames = filter_filenames(
            include_analysis["files"],
            filename_filter=FILENAME_FILTER_REGEX,
            filter_generated_files=True,
            filter_mojom_headers=True,
            filter_third_party=True,
        )

        prevalence_buckets = {bucket: 0 for bucket in buckets}

        for filename in filenames:
            prevalence = include_analysis["prevalence"][filename]
            prevalence = (100.0 * prevalence) / root_count

            for bucket in prevalence_buckets:
                if prevalence >= bucket:
                    logging.debug(f"{filename} has {bucket}%+ prevalence")
                    prevalence_buckets[bucket] += 1

        data = [prevalence_buckets[bucket] for bucket in prevalence_buckets]
        yield (include_analysis["date"], *data)


def main():
    from tqdm import tqdm
    from tqdm.contrib.logging import logging_redirect_tqdm

    parser = argparse.ArgumentParser(description="Track overall header prevalence over time")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", default=False, help="Include all archived analysis, including multiple per day.")
    group.add_argument("--buckets", default="10,20,30,40,50,60", help="Comma-separated list of prevalence buckets.")
    group.add_argument("--quiet", action="store_true", default=False, help="Only log warnings and errors.")
    group.add_argument("--verbose", action="store_true", default=False, help="Enable verbose logging.")
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(message)s",
        level=logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO,
    )

    analysis_list = extract_include_analysis_list()
    buckets = tuple(map(float, args.buckets.split(",")))

    csv_writer = csv.writer(sys.stdout)
    csv_writer.writerow(("date", *(f"{str(bucket).removesuffix('.0')}%" for bucket in buckets)))

    try:
        with logging_redirect_tqdm(), tqdm(total=len(analysis_list), unit="analysis") as progress_output:
            for row in get_prevalence_over_time(analysis_list, buckets, latest_per_date=not args.all):
                csv_writer.writerow(row)
                progress_output.update()

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
