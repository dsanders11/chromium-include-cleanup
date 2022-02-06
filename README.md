# chromium-include-cleanup
Scripts to help guide cleanup of #include lines in the Chromium codebase

## Scripts

* `post_process_compilation_db.py` - Post-process the clang compilation
  database for Chromium
* `suggest_include_changes.py` - Suggests includes to add and remove

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

### Patching `clangd`

To get suggestions for includes to add, `clangd` needs to be patched with
`clangd-include-adds.patch` and built from source.

## `clangd` Configuration

You need to enable `NeededIncludes` and `UnusedIncludes` diagnostics in a
`clangd` [config file][clangd-config]:

```yaml
Diagnostics:
  NeededIncludes: Strict
  UnusedIncludes: Strict
```

## Finding Unused Includes

These instructions assume you've already built and processed the build
log with `include_analysis.py`, if you haven't, see the link above under
"Prerequisites". It assumes the output is at `~/include-analysis.js`, so
adjust to taste.

This also assumes you have `clangd` on your `$PATH`.

```shell
$ cd ~/chromium/src/out/Default
$ gn gen . --export-compile-commands
$ python3 ~/chromium-include-cleanup/post_process_compilation_db.py compile_commands.json > compile_commands-fixed.json
$ mv compile_commands-fixed.json compile_commands.json
$ cd ../../
$ python3 ~/chromium-include-cleanup/suggest_include_changes.py --compile-commands-dir=out/Default ~/include-analysis.js > unused-edges.csv
```

Another useful option is `--filename-filter=^base/`, which lets you filter the
files which will be analyzed, which can speed things up considerably if it is
limited to a subset of the codebase.

## Current Limitations

Currently the `suggest_include_changes.py` script has problems with suggesting
includes to remove when the filename in the `#include` line does not match the
filename in the include analysis output, which could happen for includes
inside third-party code which is including relative to itself, not the
Chromium src root.

## Accuracy of Output

These scripts rely on `clangd` and specifically the "IncludeCleaner" feature
to determine which includes are unused, and which headers need to be added.
With the Chromium codebase, there are many places where `clangd` will return
false positives, suggesting that an include is not used when it actually is.
As such, the output is more of a guide than something which can be used as-is
in an automated situation.

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
[clangd-config]: https://clangd.llvm.org/config#files
