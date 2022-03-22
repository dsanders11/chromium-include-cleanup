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

Flags to use when running `suggest_include_changes.py`:

```
--include-dir src
--include-dir deps/cares/include
--include-dir deps/cares/src/lib
--include-dir deps/googletest/include
--include-dir deps/icu-small/source/common
--include-dir deps/icu-small/source/i18n
--include-dir deps/icu-small/source/tools/toolutil
--include-dir deps/nghttp2/lib
--include-dir deps/ngtcp2/nghttp3/lib
--include-dir deps/ngtcp2/ngtcp2/lib
--include-dir deps/openssl/openssl/include
--include-dir deps/openssl/openssl
--include-dir deps/openssl/openssl/apps/include
--include-dir deps/v8/include
--include-dir deps/v8
--include-dir deps/uv/include
--include-dir deps/uvwasi/include
```

And use `--ignores=node` with `filter_include_changes.py`
