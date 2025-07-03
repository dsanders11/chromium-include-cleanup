# chromium-include-cleanup

Scripts to help guide cleanup of #include lines in a codebase, using `clangd`

## Scripts

* `add_to_remove_include.py` - Determine which missing include edges need to be
  added to remove a specific include
* `apply_include_changes.py` - Apply include changes to files in the source
  tree
* `extract_archived_include_analysis.py` - Extract archived include analysis JSON
* `filter_include_changes.py` - Filter include changes output
* `include_analysis_diff.py` - Analyze differences between an include analysis
  output and previous ones
* `list_includers.py` - List includers of a file
* `list_transitive_includes.py` - List transitive (and direct) includes of a file
* `post_process_compilation_db.py` - Post-process the clang compilation
  database for analysis
* `recalculate_expanded_sizes.py` - Recalculate translation unit expanded sizes if
  all provided include changes were applied
* `set_edge_weights.py` - Set edge weights in include changes output
* `suggest_include_changes.py` - Suggests includes to add and remove
* `trace_transitive_include.py` - Trace a transitive include from a source file

## Prerequisites

To use these scripts, you'll need:

* A [release of `clangd`][clangd-releases] which has "IncludeCleaner" with
  support for missing includes (17.0.0+)
* The full output of `//tools/clang/scripts/analyze_includes.py`, see
  [discussion on the mailing list][include-analysis] for how to generate it
* A compilation database for `clangd` to use, which can be generated with
  `gn gen . --export-compile-commands` in the Chromium output directory
  * The generated `compile_commands.json` should be post-processed with
    the `post_process_compilation_db.py` script for best results

### Install Dependencies

```shell
$ pip install -r ~/chromium-include-cleanup/requirements.txt
```

## `clangd` Configuration

You need to enable `MissingIncludes` and `UnusedIncludes` diagnostics in a
`clangd` [config file][clangd-config]:

```yaml
Diagnostics:
  MissingIncludes: Strict
  UnusedIncludes: Strict
```

## Finding Unused Includes

These instructions assume you've already built and processed the build
log with `//tools/clang/scripts/analyze_includes.py`, if you haven't, see the link above under
"Prerequisites". It assumes the output is at `~/include-analysis.js`, so
adjust to taste.

This also assumes you have `clangd` on your `$PATH`.

```shell
$ cd ~/chromium/src/out/Default
$ gn gen . --export-compile-commands
$ python3 ~/chromium-include-cleanup/post_process_compilation_db.py compile_commands.json > compile_commands-fixed.json
$ mv compile_commands-fixed.json compile_commands.json
$ cd ../../
$ python3 ~/chromium-include-cleanup/suggest_include_changes.py --compile-commands-dir=out/Default ~/include-analysis.js > ~/unused-edges.csv
$ python3 ~/chromium-include-cleanup/set_edge_weights.py ~/unused-edges.csv ~/include-analysis.js --config ~/chromium-include-cleanup/configs/chromium.json > ~/weighted-unused-edges.csv
```

Another useful option is `--filename-filter=^base/`, which lets you filter the
files which will be analyzed, which can speed things up considerably if it is
limited to a subset of the codebase.

Edge weights are set in a separate script to allow quick iteration, since
`suggest_include_changes.py` takes many hours to run. The default metric
for edge weights pulls the "Added Size" metric from the include analysis
output. This means new weights can be easily be applied to the output of
`suggest_include_changes.py` by downloading the latest hosted include
analysis output at <https://commondatastorage.googleapis.com/chromium-browser-clang/include-analysis.js>,
but mileage may vary since you're combining output from your local build
and the hosted build.

## Performance

For a full codebase run of the `suggest_include_changes.py` script on Ubuntu,
it takes 7 hours on a 4 core, 8 thread machine. `clangd` is highly parallel
though, and the script is configured to use all available logical CPUs, so it
will scale well on beefier machines.

## Current Limitations

Currently the `suggest_include_changes.py` script has problems with suggesting
includes to remove when the filename in the `#include` line does not match the
filename in the include analysis output, which could happen for includes
inside third-party code which is including relative to itself, not the source
root.

When suggesting includes to add, `clangd` will sometimes suggest headers which
are internal to the standard library, like `<__hash_table>`, rather than the
public header. Unfortunately these cases can't be disambiguated by this script,
since there's not enough information to work off of.

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
* Forward declarations in the file being analyzed
  * `clangd` won't consider an include unused even if forward declarations
    exist which make it unnecessary
  * `clangd` will still suggest an include even if a forward declaration makes it
    unnecessary
  * In some circumstances the presence of an incorrect forward declaration
    will stop `clangd` from suggesting a missing include

[clangd-releases]: https://github.com/clangd/clangd/releases
[include-analysis]: https://groups.google.com/a/chromium.org/g/chromium-dev/c/0ZME4DuE06k
[clangd-config]: https://clangd.llvm.org/config#files
