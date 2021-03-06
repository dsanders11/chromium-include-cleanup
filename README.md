# chromium-include-cleanup

Scripts to help guide cleanup of #include lines in a codebase, using `clangd`

## Scripts

* `apply_include_changes.py` - Apply include changes to files in the source
  tree
* `filter_include_changes.py` - Filter include changes output
* `post_process_compilation_db.py` - Post-process the clang compilation
  database for analysis
* `set_edge_weights.py` - Set edge weights in include changes output
* `suggest_include_changes.py` - Suggests includes to add and remove

## Prerequisites

To use these scripts, you'll need:

* An [release of `clangd`][clangd-releases] which has "IncludeCleaner"
  (14.0.0+), or a build of `clangd` from source with the patches in this
  repository applied for expanded functionality.
* The full output of `include_analysis.py`, see
  [discussion on the mailing list][include-analysis] for how to generate it
* A compilation database for `clangd` to use, which can be generated with
  `gn gen . --export-compile-commands` in the Chromium output directory
  * The generated `compile_commands.json` should be post-processed with
    the `post_process_compilation_db.py` script for best results

### Install Dependencies

```
$ pip install -r ~/chromium-include-cleanup/requirements.txt
```

### Patching `clangd`

To get suggestions for includes to add, and other tweaks, `clangd` needs to be
patched with the patches in `clangd_patches` and built from source.

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

[clangd-releases]: https://github.com/clangd/clangd/releases
[include-analysis]: https://groups.google.com/a/chromium.org/g/chromium-dev/c/0ZME4DuE06k
[clangd-config]: https://clangd.llvm.org/config#files
