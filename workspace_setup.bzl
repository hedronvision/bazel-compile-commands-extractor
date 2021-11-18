# Do not change the filename; it is part of the user interface.

def hedron_compile_commands_setup():
    """Set up a WORKSPACE to have hedron_compile_commands used within it."""

    # Unified setup for users' WORKSPACES and this workspace when used standalone.
    # See invocations in:
    #     README.md (for users)
    #     WORKSPACE (for working on this repo standalone)

    # Currently nothing to do -> no-op.
    # So why is this even here? Enables future expansion (e.g to add transitive dependencies) without changing the user interface.
    pass
