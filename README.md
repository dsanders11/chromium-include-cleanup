# chromium-include-cleanup
Scripts to help guide cleanup of #include lines in the Chromium codebase

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
