# chromium-include-cleanup
Scripts to help guide cleanup of #include lines in the Chromium codebase

## Prerequisites

To use these scripts, you'll need:

* An [unstable snapshot release][clangd-releases] of `clangd` which has
  "IncludeCleaner"
  * During development, snapshot 20211205 was used
* The full output of `include_analysis.py`, see
  [discussion on the mailing list][include-analysis] for how to generate it
* A compilation database for `clangd` to use, which can be generated with
  `gn gen . --export-compile-commands` in the Chromium output directory
  * The generated `compile_commands.json` should be post-processed with
    the `post_process_compilation_db.py` script for best results

## `clangd` Configuration

TODO

## Finding Unused Includes

TODO

## Current Limitations

Currently the `find_unused_edges.py` script has problems with generated output
files since the filename used in the `#include` line does not match what is
found in the include analysis output. This also affects parts of the codebase
where includes are relative to a subdirectory, since again the filenames will
not match. This can likely be improved with a bit of effort.

## Accuracy of Output

These scripts rely on `clangd` and specifically the "IncludeCleaner" feature
to determine which includes are unused. With the Chromium codebase, there are
many places where `clangd` will return false positives, suggesting that an
include is not used when it actually is. As such, the output is more of a
guide than something which can be used as-is in an automated situation.

Known situations in Chromium where `clangd` will produce false positives:

* When an include is only used for a `friend class` declaration
* When the code using an include is inside an `#ifdef` not used on the system
  which built the codebase
* Macros in general are often a struggle point
* Umbrella headers
* Certain forward declarations seem to be flagged incorrectly as the canonical
  location for a symbol, such as "base/callback_forward.h"


[clangd-releases]: https://github.com/clangd/clangd/releases
[include-analysis]: https://groups.google.com/a/chromium.org/g/chromium-dev/c/0ZME4DuE06k
