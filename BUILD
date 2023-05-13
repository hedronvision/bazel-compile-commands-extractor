load(":refresh_compile_commands.bzl", "refresh_compile_commands")

# See README.md for interface.

# But if you aren't doing any cross-compiling for other platforms, the following can be a good default:
# bazel run @hedron_compile_commands//:refresh_all
refresh_compile_commands(
    name = "refresh_all",
)


########################################
# Implementation:
# If you are looking into the implementation, start with the overview in ImplementationReadme.md.

exports_files(["refresh.template.py", "check_python_version.template.py"])  # For implicit use by therefresh_compile_commands macro, not direct use.
