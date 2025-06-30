import json
import re
from typing import Dict, List, Optional, TypedDict

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
