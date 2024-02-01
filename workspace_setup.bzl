# Do not change the filename; it is part of the user interface.


load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")
load("@bazel_tools//tools/build_defs/repo:utils.bzl", "maybe")


def hedron_compile_commands_setup(module_ctx = None):
    """Set up a WORKSPACE to have hedron_compile_commands used within it.

    module_ctx is for automatic-implicit use by bzlmod only.
    """

    # Unified setup for users' WORKSPACES and this workspace when used standalone.
    # See invocations in:
    #     README.md (for WORKSPACE users)
    #     MODULE.bazel (for bzlmod users and for working on this repo standalone)

    # If adding dependencies available via bzlmod, consider adding them to MODULE.bazel, too, and only loading them the WORKSPACE way when needed. For example:
    # if not module_ctx:
    #   # Load bzlmod-available packages.

    # Currently nothing to do -> no-op.
    # So why is this even here? Enables future expansion (e.g to add transitive dependencies) without changing the user interface.
    pass


hedron_compile_commands_extension = module_extension( # Note: Doesn't break loading from WORKSPACE as far back as Bazel 5.0.0
    implementation = hedron_compile_commands_setup,
    # This extension is automatically loaded when using bzlmod (from MODULE.bazel) and will run the same function as WORKSPACE,
    # but passes in a module_ctx object for advanced context of the whole project, allowing for complex, project wide modifiying extensions and distinguishing between WORKSPACE and bzlmod setups.
)
