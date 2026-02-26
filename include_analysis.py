import json
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, TypedDict

DATA_REGEX = re.compile(r".*<script>\n?(data = .*?)<\/script>", re.DOTALL)
GENERATED_FILE_PREFIX_REGEX = re.compile(r"^(out/[\w-]+/gen/).*$")
SYSROOT_REGEX = re.compile(r"^(build/linux/[\w-]+/)usr/include/([\w-]+)/sys/.*$")


class RawIncludeAnalysisOutput(TypedDict):
    revision: str
    date: str
    files: List[str]
    roots: List[int]
    includes: List[List[int]]
    included_by: List[List[int]]
    sizes: List[int]
    tsizes: List[int]
    asizes: List[int]
    esizes: List[List[int]]
    prevalence: List[int]


class IncludeAnalysisOutput(TypedDict):
    revision: str
    date: str
    files: List[str]
    roots: List[str]
    includes: Dict[str, List[str]]
    included_by: Dict[str, List[str]]
    sizes: Dict[str, int]
    tsizes: Dict[str, int]
    asizes: Dict[str, int]
    esizes: Dict[str, Dict[str, int]]
    prevalence: Dict[str, int]
    gen_prefix: str
    sysroot: str
    sysroot_platform: str


class ParseError(Exception):
    pass


def parse_raw_include_analysis_output(output: str) -> Optional[IncludeAnalysisOutput]:
    """
    Parses the raw output JavaScript file from the include analysis script and expands it

    Converts the file numbers to the full filename strings to make it easier to work with,
    and also converts the various keys back into full dicts.
    """
    try:
        # TODO - Validate with JSON Schema?
        match = re.match(r"data = ({.*})", output)
        if match:
            raw_output: RawIncludeAnalysisOutput = json.loads(match.group(1))
        else:
            raise ParseError()
    except json.JSONDecodeError as e:
        raise ParseError(str(e)) from e

    parsed_output: IncludeAnalysisOutput = raw_output.copy()

    files = raw_output["files"]

    # "roots" is a list of root filenames
    parsed_output["roots"] = [files[nr] for nr in raw_output["roots"]]

    # "includes" is a dict of filename to a list of included filenames
    parsed_output["includes"] = {
        files[nr]: [files[nr] for nr in includes] for nr, includes in enumerate(raw_output["includes"])
    }

    # "included_by" is a dict of filename to a list of filenames which include it
    parsed_output["included_by"] = {
        files[nr]: [files[nr] for nr in included_by] for nr, included_by in enumerate(raw_output["included_by"])
    }

    # "sizes" is a dict of filename to the size of the file in bytes
    # "tsizes" is a dict of filename to the expanded size of the file in bytes
    # "asizes" is a dict of filename to the added size of the file in bytes
    for key in ("sizes", "tsizes", "asizes"):
        parsed_output[key] = {files[nr]: size for nr, size in enumerate(raw_output[key])}  # type: ignore

    # "esizes" is a dict of includer filename to a nested dict of included filename to added size in bytes
    parsed_output["esizes"] = {
        files[nr]: {parsed_output["includes"][files[nr]][idx]: asize for idx, asize in enumerate(includes)}
        for nr, includes in enumerate(raw_output["esizes"])
    }

    # "prevalence" is a dict of filename to the total number of occurrences
    parsed_output["prevalence"] = {files[nr]: prevalence for nr, prevalence in enumerate(raw_output["prevalence"])}

    # Determine the generated file prefix from the files list (e.g. "out/linux-Debug/gen/")
    gen_prefix = None

    for filename in files:
        match = GENERATED_FILE_PREFIX_REGEX.match(filename)

        if match:
            gen_prefix = match.group(1)
            break

    if gen_prefix is None:
        raise RuntimeError("Could not determine generated file prefix from include analysis output")

    parsed_output["gen_prefix"] = gen_prefix

    # Determine the sysroot from the files list (e.g. "build/linux/debian_bullseye_amd64-sysroot/")
    sysroot = None
    sysroot_platform = None

    for filename in files:
        match = SYSROOT_REGEX.match(filename)

        if match:
            sysroot = match.group(1)
            sysroot_platform = match.group(2)
            break

    if sysroot is None or sysroot_platform is None:
        raise RuntimeError("Could not determine sysroot from include analysis output")

    parsed_output["sysroot"] = sysroot
    parsed_output["sysroot_platform"] = sysroot_platform

    return parsed_output


def get_latest_include_analysis():
    cached_file_path = Path(__file__).resolve().parent.joinpath(".cached-include-analysis")
    url = "https://commondatastorage.googleapis.com/chromium-browser-clang/include-analysis.js"

    cache_max_age_seconds = 10 * 60  # 10 minutes

    etag = None
    raw_include_analysis = None

    if cached_file_path.exists():
        with open(cached_file_path, "r") as f:
            [etag, raw_include_analysis] = f.read().split("\n", 1)

        # If cache is less than 10 minutes old, use it directly
        cache_age = time.time() - cached_file_path.stat().st_mtime
        if cache_age < cache_max_age_seconds:
            return raw_include_analysis

    try:
        # Make request with ETag if available
        request = urllib.request.Request(url)
        if etag:
            request.add_header("If-None-Match", etag)

        response = urllib.request.urlopen(request)

        # If we get here, there's new content (200 OK)
        raw_include_analysis = response.read().decode("utf8")

        # Save the new content with ETag on first line
        new_etag = response.headers.get("ETag", "")
        with open(cached_file_path, "w") as f:
            f.write(f"{new_etag}\n{raw_include_analysis}")
    except urllib.error.HTTPError as e:
        # If not "304 Not Modified", fall back to cache if available, else raise the error
        if e.code == 304:
            # Content unchanged, update mtime so we don't check again for 10 minutes
            cached_file_path.touch(exist_ok=True)
        elif not raw_include_analysis:
            raise
    except urllib.error.URLError:
        # If there's a network error, fall back to cache if available, else raise the error
        if not raw_include_analysis:
            raise

    return raw_include_analysis


def extract_include_analysis(contents: str) -> str:
    data_match = DATA_REGEX.match(contents)

    if data_match:
        return data_match.group(1).strip()

    return ""


def load_include_analysis(include_analysis_path: Optional[str]) -> IncludeAnalysisOutput:
    # If the user specified an include analysis output file, use that instead of fetching it
    if include_analysis_path:
        if include_analysis_path.startswith("https://"):
            include_analysis_response = urllib.request.urlopen(include_analysis_path)
            include_analysis_contents = include_analysis_response.read().decode("utf8")
            raw_include_analysis = extract_include_analysis(include_analysis_contents)

            if not raw_include_analysis:
                raise RuntimeError(f"Could not extract include analysis from {include_analysis_path}")
        elif include_analysis_path == "-":
            raw_include_analysis = sys.stdin.read()
        else:
            with open(include_analysis_path, "r") as f:
                raw_include_analysis = f.read()
    else:
        raw_include_analysis = get_latest_include_analysis()

    return parse_raw_include_analysis_output(raw_include_analysis)
