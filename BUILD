load("refresh_compile_commands.bzl", "refresh_compile_commands")

# See README.md for interface.

# But if you aren't doing any cross-compiling for other platforms, the following can be a good default:
# bazel run @hedron_compile_commands//:refresh_all
refresh_compile_commands(
    name = "refresh_all",
)


########################################
# Implementation:
# If you are looking into the implementation, start with the overview in ImplementationReadme.md.

exports_files(["refresh.sh.template"]) # For implicit use by refresh_compile_commands.

# :extract is meant to be called from Refresh.sh. Work off the invocation there.
py_binary(
    name = "extract",
    srcs = ["extract.py"],
)
