import time
from typing import List

import sansio_lsp_client as lsp

# This is a list of known filenames where clangd produces a
# false positive when suggesting unused includes to remove
UNUSED_INCLUDE_IGNORE_LIST = [
    "build/build_config.h",
    # TODO - Populate this list
]


def get_unused_includes(filename: str) -> List[str]:
    """Returns a list of unused includes for a filename"""
    time.sleep(1)  # TODO - Implement

    return []
