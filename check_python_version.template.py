"""
Print a nice error message if the user is running too old of a version of python.

Why not just put this block at the top of refresh.template.py?
Python versions introduce constructs that don't parse in older versions, leading to an error before the version check is executed, since python parses files eagerly.
For examples of this issue, see https://github.com/hedronvision/bazel-compile-commands-extractor/issues/119 and https://github.com/hedronvision/bazel-compile-commands-extractor/issues/95
This seems common enough that hopefully bazel will support it someday. We've filed a request: https://github.com/bazelbuild/bazel/issues/18389
"""

import sys
if sys.version_info < (3,6):
    sys.exit("\n\033[31mFATAL ERROR:\033[0m Python 3.6 or later is required. Please update!")

# Only import -> parse once we're sure we have the required python version
import {to_run}
{to_run}.main()
