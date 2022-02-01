import json
import re
from typing import Dict, List, TypedDict


class IncludeAnalysisOutput(TypedDict):
    roots: List[str]
    includes: Dict[str, List[str]]
    included_by: Dict[str, List[str]]
    sizes: Dict[str, int]
    tsizes: Dict[str, int]
    asizes: Dict[str, int]
    esizes: Dict[str, Dict[str, int]]
    prevalence: Dict[str, int]


def parse_raw_include_analysis_output(output: str) -> IncludeAnalysisOutput:
    """
    Parses the raw output JavaScript file from the include analysis script and expands it

    Converts the file numbers to the full filename strings to make it easier to work with,
    and also converts the various keys back into full dicts.
    """
    try:
        # TODO - Validate with JSON Schema?
        parsed_output: dict = json.loads(re.match(r"data = ({.*})", output).group(1))
    except json.JSONDecodeError:
        return None

    # Nothing needs to be done with "files", it's already just a list of filenames
    files = parsed_output["files"]

    # "roots" is a list of root filenames
    parsed_output["roots"] = [files[nr] for nr in parsed_output["roots"]]

    # "includes" is a dict of filename to a list of included filenames
    parsed_output["includes"] = {
        files[nr]: [files[nr] for nr in includes] for nr, includes in enumerate(parsed_output["includes"])
    }

    # "included_by" is a dict of filename to a list of filenames which include it
    parsed_output["included_by"] = {
        files[nr]: [files[nr] for nr in included_by] for nr, included_by in enumerate(parsed_output["included_by"])
    }

    # "sizes" is a dict of filename to the size of the file in bytes
    # "tsizes" is a dict of filename to the expanded size of the file in bytes
    # "asizes" is a dict of filename to the added size of the file in bytes
    for key in ("sizes", "tsizes", "asizes"):
        parsed_output[key] = {files[nr]: size for nr, size in enumerate(parsed_output[key])}

    # "esizes" is a dict of includer filename to a nested dict of included filename to added size in bytes
    parsed_output["esizes"] = {
        files[nr]: {parsed_output["includes"][files[nr]][idx]: asize for idx, asize in enumerate(includes)}
        for nr, includes in enumerate(parsed_output["esizes"])
    }

    # "prevalence" is a dict of filename to the total number of occurrences
    parsed_output["prevalence"] = {files[nr]: prevalence for nr, prevalence in enumerate(parsed_output["prevalence"])}

    return parsed_output
