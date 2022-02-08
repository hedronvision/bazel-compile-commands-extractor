""" refresh_compile_commands rule

When `bazel run`, these rules refresh the compile_commands.json in the root of your Bazel workspace
    [creating compile_commands.json if it doesn't already exist.]

Best explained by concrete example--copy the following and follow the comments:
```Starlark
load("@hedron_compile_commands//:refresh_compile_commands.bzl", "refresh_compile_commands")

refresh_compile_commands(
    name = "refresh_compile_commands",

    # Specify the targets of interest.
        # This will create compile commands entries for all the code compiled by those targets, including transitive dependencies.
        # It's optional, but if you're reading this, you probably want to. 
    # Usually, you'll want to specify the output targets you care about.
        # This avoids issues where some targets can't be built on their own; they need configuration by a parent rule. android_binaries using transitions to configure android_libraries are an example.
    # The targets parameter is forgiving in its inputs.
        # You can specify just one target:
            # targets = "//:my_output_binary_target",
        # Or a list of targets:
            # targets = ["//:my_output_1", "//:my_output_2"],
        # Or a dict of targets and their arguments:
            # targets = {
            #   "//:my_output_1": "--important_flag1 --important_flag2=true", 
            #   "//:my_output_2": "",
            # },
        # If you don't specify a target, that's fine (if it works for you); compile_commands.json will default to containing commands used in building all possible targets. But in that case, just bazel run @hedron_compile_commands//:refresh_all
        # Wildcard target patterns (..., *, :all) patterns are allowed, like in bazel build:
            # For more, see https://docs.bazel.build/versions/main/guide.html#specifying-targets-to-build
)
```
"""

def refresh_compile_commands(name, targets = None):
    """
    Wrapper that converts various acceptable target types into a common format

    Args:
      targets: Optional string/list/dict of targets as specified in the module-docstring.
    """
    if not targets: # Default to all targets in main workspace
        targets = {"@//...": ""}
    elif type(targets) == "list": # Allow specifying a list of targets w/o arguments
        targets = {target: "" for target in targets}
    elif type(targets) != "dict": # Assume they've supplied a single string/label and wrap it 
        targets = {targets: ""}

    script_name = name + ".py"
    _expand_template(name = script_name, labels_to_flags = targets)
    native.py_binary(name=name, srcs=[script_name])


def _expand_template_impl(ctx):
    """
    Inject targets of interest into refresh.template.py, and set it up to be run.
    """
    script = ctx.actions.declare_file(ctx.attr.name)
    ctx.actions.expand_template(
        output = script,
        is_executable = True,
        template = ctx.file._script_template,
        substitutions = {"        {get_commands}": "\n".join(["        (%r, %r)," % p for p in ctx.attr.labels_to_flags.items()])}
    )
    return DefaultInfo(files=depset(direct=[script]))

_expand_template = rule(
    attrs = {
        "labels_to_flags": attr.string_dict(mandatory = True), # string keys instead of label_keyed because Bazel doesn't support parsing wildcard target patterns (..., *, :all) in BUILD attributes.
        "_script_template": attr.label(allow_single_file = True, default = "refresh.template.py")
    },
    implementation = _expand_template_impl
)
