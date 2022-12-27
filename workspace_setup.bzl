# Do not change the filename; it is part of the user interface.
load("//:private/util.bzl", _write_install_config = "write_install_config")

def hedron_compile_commands_setup(install_config = {}):
    """Set up a WORKSPACE to have hedron_compile_commands used within it."""

    # Unified setup for users' WORKSPACES and this workspace when used standalone.
    # See invocations in:
    #     README.md (for users)
    #     WORKSPACE (for working on this repo standalone)

    _write_install_config(
        name = "hedron_compile_commands_install_config",
        install_config = install_config,
    )
