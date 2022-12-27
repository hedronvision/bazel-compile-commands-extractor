"""
Utility functions for reading/writing workspace setup information.
"""

def write_dict_entries(d, indent_level = 1):
    """Write the entries of dictionary to a string."""
    return "\n".join([
        "{indent}\"{key}\": \"{value}\",".format(
            indent = indent_level * 4 * " ",
            key = key,
            value = value,
        )
        for key, value in d.items()
    ])

def _write_install_config_impl(rctx):
    rctx.file("WORKSPACE.bazel", executable = False)
    rctx.file("BUILD.bazel", executable = False)

    entries = write_dict_entries(rctx.attr.install_config)

    rctx.file(
        "config.bzl",
        content = """
INSTALL_CONFIG = {{
{entries}
}}
""".format(entries = entries),
        executable = False,
    )

write_install_config = repository_rule(
    implementation = _write_install_config_impl,
    attrs = {
        "install_config": attr.string_dict(
            doc = "Installation configuration parameters (e.g. gitignore file).",
        ),
    },
)
