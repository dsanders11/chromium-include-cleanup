# Using with Node.js

These scripts have been successfully used to analyze Node.js on Linux.
Although `clang` is not listed as a supported build system for Node.js on
Linux, it works in practice. The following can be used to build Node.js in a
way that will output the necessary build log info for being analyzed by
`analyze_includes.py`.

## Dependencies

You'll need clang 13+, which you can get by following instructions on the LLVM
site: https://apt.llvm.org/

You need to use [`bear`](https://github.com/rizsotto/Bear) to generate
`compile-commands.json` since Node.js doesn't use `gn`.

## Usage

```
cd node
CC=clang-13 CXX=clang++-13 LINK=clang++ CFLAGS="-H -fshow-skipped-includes" CXXFLAGS="-H -fshow-skipped-includes" ./configure --ninja
bear ninja -v -C out/Release > build-log.txt
mv compile_commands.json out/Release
```

And use `--config=node` with `filter_include_changes.py`
