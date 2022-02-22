# chromium-include-cleanup
Scripts to help guide cleanup of #include lines in the Chromium codebase

## Scripts

* `apply_include_changes.py` - Apply include changes to files in the source
  tree
* `check_cl.py` - Proof-of-concept script to check if a CL should add or
  remove includes as a result of the changes being made
* `filter_include_changes.py` - Filter include changes output
* `post_process_compilation_db.py` - Post-process the clang compilation
  database for Chromium
* `suggest_include_changes.py` - Suggests includes to add and remove
* `update_edge_sizes.py` - Update edge sizes in include changes output

## Prerequisites

To use these scripts, you'll need:

* An [unstable snapshot release][clangd-releases] of `clangd` which has
  "IncludeCleaner", or a build of `clangd` from source with the patch in
  this repository applied.
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

## Performance

For a full codebase run of the `suggest_include_changes.py` script on Ubuntu,
it takes 7 hours on a 4 core, 8 thread machine. `clangd` is highly parallel
though, and the script is configured to use all available logical CPUs, so it
will scale well on beefier machines.

## Current Limitations

Currently the `suggest_include_changes.py` script has problems with suggesting
includes to remove when the filename in the `#include` line does not match the
filename in the include analysis output, which could happen for includes
inside third-party code which is including relative to itself, not the
Chromium src root.

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
  * `clangd` will still suggest an include ven if a foward declaration makes it
    unnecessary

## Checking a CL (Proof-of-Concept)

The `check_cl.py` script can be used to check if a CL should add or remove
includes. It should be run (ideally) in a source tree on the commit the CL is
on top of, but any commit approximately around that commit should still yield
results. For ideal results, the compilation database should always be
re-generated when the source tree is changed to a different HEAD commit.

```
$ cd ~/chromium/src
$ python3 ~/chromium-include-cleanup/check_cl.py --compile-commands-dir=out/Default 3433015
add,ui/base/ui_base_features.cc,base/metrics/field_trial_params.h
```

[clangd-releases]: https://github.com/clangd/clangd/releases
[include-analysis]: https://groups.google.com/a/chromium.org/g/chromium-dev/c/0ZME4DuE06k
[clangd-config]: https://clangd.llvm.org/config#files
