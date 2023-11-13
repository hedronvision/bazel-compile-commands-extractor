# Do not change the filename; it is part of the user interface.

def hedron_compile_commands_setup(ctx = None):
    """Set up a WORKSPACE to have hedron_compile_commands used within it."""

    # Unified setup for users' WORKSPACES and this workspace when used standalone.
    # See invocations in:
    #     README.md (for users)
    #     WORKSPACE (for working on this repo standalone)

    # Currently nothing to do -> no-op.
    # So why is this even here? Enables future expansion (e.g to add transitive dependencies) without changing the user interface.
    pass

hedron_compile_commands_extension = module_extension(
    implementation = hedron_compile_commands_setup,
    # This extension is loaded when using bzlmod (MODULE.bazel) and will run the same command as WORKSPACE,
    # but passes in a module_ctx object for advanced context of the whole project, allowing for complex, project wide modifiying extensions
    # It's not currently used but is always passed in. ctx is defaulted to None for compatibility with workspace setup.
)
