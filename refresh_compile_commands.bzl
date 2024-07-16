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
            # If you're working on a header-only library, specify a test or binary target that compiles it.
    # If you're doing this manually, you usually want to just specify the top-level, output targets you care about.
        # This avoids issues where some targets can't be built on their own; they need configuration by a parent rule. android_binaries using transitions to configure android_libraries are an example.
    # The targets parameter is forgiving in its inputs.
        # You can specify just one target:
            # targets = "//:my_output_binary_target",
        # Or a list of targets:
            # targets = ["//:my_output_1", "//:my_output_2"],
        # Or a dict of targets and any flags required to build:
            # (No need to add flags already in .bazelrc. They're automatically picked up.)
            # targets = {
            #   "//:my_output_1": "--important_flag1 --important_flag2=true",
            #   "//:my_output_2": "",
            # },
        # If you don't specify a target, that's fine (if it works for you); compile_commands.json will default to containing commands used in building all possible targets. But in that case, just bazel run @hedron_compile_commands//:refresh_all
        # Wildcard target patterns (..., *, :all) patterns *are* allowed, like in bazel build
            # For more, see https://docs.bazel.build/versions/main/guide.html#specifying-targets-to-build
        # As are additional targets (+) and subtractions (-), like in bazel query https://docs.bazel.build/versions/main/query.html#expressions

    # Using ccls or another tool that doesn't want or need headers in compile_commands.json?
        # exclude_headers = "all", # By default, we include entries for headers to support clangd, working around https://github.com/clangd/clangd/issues/123
        # ^ excluding headers will speed up compile_commands.json generation *considerably* because we won't need to preprocess your code to figure out which headers you use.
        # However, if you use clangd and are looking for speed, we strongly recommend you follow the instructions below instead, since clangd is going to regularly infer the wrong commands for headers and give you lots of annoyingly unnecessary red squigglies.

    # Need to create separate files for specific targets? Give those targets a name and their compile commands file will be written into a subdirectory with that name.
        # target_groups = {
        #     "host": "//:host_build",
        #     "target": "//:target_build",
        # }

    # You can define target groups, sort of like "meta-targets" that contain combination of compile commands from the specified targets.
    # This is useful for multi-architecture projects, where you might be building the same files in multiple different ways depending on what architecture or device the code will run on.
    # It only makes sense to get code intelligence for a consistent build, so you can produce consistent compile commands with target groups.
    # Targets only need to be specified in either `targets` or `target_groups`; there's no need to add them to both.
        # target_groups = {
        #     "host": [
        #         "//:host_build",
        #         "//:host_tests",
        #     ],
        #     "target": [
        #         ["//:target_build", "--platform=//:target_device --important_flag"],
        #     ],
        # }


    # Need to write compile commands to some directory other than the workspace root? Provide a path relative to the workspace root. This is useful if you use target groups.
        # out_dir = ".compile_commands"

    # Need things to run faster? [Either for compile_commands.json generation or clangd indexing.]
    # First: You might be able to refresh compile_commands.json slightly less often, making the current runtime okay.
        # If you're adding files, clangd should make pretty decent guesses at completions, using commands from nearby files. And if you're deleting files, there's not a problem. So you may not need to rerun refresh.py on every change to BUILD files. Instead, maybe refresh becomes something you run every so often when you can spare the time, making the current runtime okay.
        # If that's not enough, read on.
    # If you don't care about the implementations of external dependencies:
        # Then skip adding entries for compilation in external workspaces with
        # exclude_external_sources = True,
        # ^ Defaults to False, so the broadest set of features are supported out of the box, without prematurely optimizing.
    # If you don't care about browsing headers from external workspaces or system headers, except for a CTRL/CMD+click every now and then:
        # Then no need to add entries for their headers, because clangd will correctly infer from the CTRL/CMD+click (but not a quick open or reopen).
        # exclude_headers = "external",
    # Still not fast enough?
        # Make sure you're specifying just the targets you care about by setting `targets`, above.
```
"""


########################################
# Implementation

load("@bazel_tools//tools/cpp:toolchain_utils.bzl", "find_cpp_toolchain")


def refresh_compile_commands(
        name,
        targets = None,
        target_groups = None,
        out_dir = None,
        exclude_headers = None,
        exclude_external_sources = False,
        **kwargs):  # For the other common attributes. Tags, compatible_with, etc. https://docs.bazel.build/versions/main/be/common-definitions.html#common-attributes.

    # Given `targets` that may be absent, or be a single string target:
    #    targets = "//:some_target"
    #
    # ... or be a list of string targets:
    #    targets = [
    #        "//:some_target",
    #        "//:another_target"
    #    ]
    #
    # ... or be a dict of string targets and build flags:
    #    targets = {
    #        "//:some_target": "--flag ...",
    #        "//:another_target": "--arg ..."
    #    }
    #
    # And given `target_groups` that may be absent, or have a group name associated with a string target or list of string targets:
    #    target_groups = {
    #        "host": "//:host_target",
    #        "device": [
    #           "//:device_target",
    #           ...
    #        ]
    #    }
    #
    # ... or have group names associated with lists of string targets with build flags:
    #    target_groups = {
    #        "host": [
    #           ("//:host_target", "--flag ..."),
    #           ("//:test_target", "--platform=//:host_platform"),
    #           ...
    #        ],
    #        "device": [
    #           ("//:device_target", "--arg ..."),
    #           ("//:test_target", "--platform=//:device_platform"),
    #           ...
    #        ]
    #    }
    #
    # ... we want to produce a `string_dict_list` that we can pass into the Python script template to assemble the `target_flag_pairs`.
    # A simple dict with target name keys isn't adequate, because we can specify a target more than once with different build flags (see the last example above).
    # So we assemble a dict where the keys are unique to each target+flag combination, and the values are a list of strings in this format: [<target>, <flags>]
    #
    # That takes care of the `target_flag_pairs`, which determines which targets to generate compile commands for.
    #
    # Associating compile commands with target groups is easier; we pass `target_groups`, but with the target names and flags concatenated in the same way
    # as described above. This lets us associate each target+flag combination we generate compile commands for with its membership(s) in target groups.

    target_group_targets_list = []
    serialized_target_groups = {}

    if target_groups:
        # Convert the targets specified in `target_groups` into the format we'll use with `targets`, so we can combine them into one list of targets to generate compile commands for.
        for targets_for_group in target_groups.values():
            # You can specify a bare string target if the group only has one target.
            if type(targets_for_group) != "list":
                targets_for_group = [targets_for_group]

            for target in targets_for_group:
                # The target can either be a plain string target name, or a tuple of a target name and build flags, similar to the format of `targets`.
                if type(target) == "list":
                    target_name, flags = target
                else:
                    target_name = target
                    flags = ""

                target_data = (_make_label_absolute(target_name), flags)

                # Targets may appear in multiple groups. We don't want duplicates in the final list, but Starlark doesn't have Python's set class. So we de-duplicate manually.
                if target_data not in target_group_targets_list:
                    target_group_targets_list.append(target_data)

        # Assemble the association between target groups and their targets.
        for group, targets_for_group in target_groups.items():
            serialized_targets = []

            for target in targets_for_group:
                if type(target) == "list":
                    # If the target has flags, concat them with the target name. That's how we'll associate compile commands with groups on the Python side.
                    serialized_targets.append("".join(target))
                else:
                    serialized_targets.append(target)

            serialized_target_groups[group] = serialized_targets

    target_group_targets = {"{}{}".format(target, flags): [target, flags] for target, flags in target_group_targets_list}

    # Convert the various, acceptable target shorthands into the dictionary format
    # In Python, `type(x) == y` is an antipattern, but [Starlark doesn't support inheritance](https://bazel.build/rules/language), so `isinstance` doesn't exist, and this is the correct way to switch on type.
    if not targets and not target_groups:  # Default to all targets in main workspace
        targets = {"@//...": ["@//...", ""]}
    elif not targets:  # In this case, targets were defined only in `target_groups`
        targets = target_group_targets
    elif type(targets) == "select":  # Allow select: https://bazel.build/reference/be/functions#select
        # Pass select() to _expand_template to make it work
        # see https://bazel.build/docs/configurable-attributes#faq-select-macro
        pass
    elif type(targets) == "list":  # Allow specifying a list of targets w/o arguments
        absolute_targets = [_make_label_absolute(target) for target in targets]
        targets = {target: [target, ""] for target in absolute_targets} | target_group_targets
    elif type(targets) != "dict":  # Assume they've supplied a single string/label and wrap it
        targets = {targets: [targets, ""]} | target_group_targets
    else:  # Assume that they've provided a dict of targets with flags
        absolute_targets = {_make_label_absolute(target): flags for target, flags in targets.items()}
        targets = {"{}{}".format(target, flags): [target, flags] for target, flags in absolute_targets.items()} | target_group_targets

    # Create a wrapper script that prints a helpful error message if the python version is too old, generated from check_python_version.template.py
    version_checker_script_name = name + ".check_python_version.py"
    _check_python_version(name = version_checker_script_name, to_run = name)

    # Generate the core, runnable python script from refresh.template.py
    script_name = name + ".py"
    _expand_template(name = script_name, labels_to_flags = targets, labels_to_groups = serialized_target_groups, out_dir = out_dir, exclude_headers = exclude_headers, exclude_external_sources = exclude_external_sources, **kwargs)

    # Combine them so the wrapper calls the main script
    native.py_binary(
        name = name,
        main = version_checker_script_name,
        srcs = [version_checker_script_name, script_name],
        data = ["@hedron_compile_commands//:print_args"],
        imports = [''], # Allows binary to import templated script, even if this macro is being called inside a sub package. See https://github.com/hedronvision/bazel-compile-commands-extractor/issues/137
        **kwargs
    )

def _make_label_absolute(label):
    # Make any package-relative labels absolute
    return label if label.startswith("/") or label.startswith("@") else "{}//{}:{}".format(native.repository_name(), native.package_name(), label.removeprefix(":"))

def _expand_target_group_targets(targets):
    return "[\n" + "\n".join(["            '{}',".format(target) for target in targets]) + "\n        ]"

def _expand_template_impl(ctx):
    """Inject targets of interest--and other settings--into refresh.template.py, and set it up to be run."""
    script = ctx.actions.declare_file(ctx.attr.name)
    ctx.actions.expand_template(
        output = script,
        is_executable = True,
        template = ctx.file._script_template,
        substitutions = {
            # Note, don't delete whitespace. Correctly doing multiline indenting.
            "        {target_flag_pairs}": "\n".join(["        {},".format(pair) for pair in ctx.attr.labels_to_flags.items()]),
            "        {target_groups}": "\n".join(["        '{}': {},".format(group_name, _expand_target_group_targets(targets)) for group_name, targets in ctx.attr.labels_to_groups.items()]),
            "        {windows_default_include_paths}": "\n".join(["        %r," % path for path in find_cpp_toolchain(ctx).built_in_include_directories]),  # find_cpp_toolchain is from https://docs.bazel.build/versions/main/integrating-with-rules-cc.html
            "{out_dir}": repr(ctx.attr.out_dir),
            "{exclude_headers}": repr(ctx.attr.exclude_headers),
            "{exclude_external_sources}": repr(ctx.attr.exclude_external_sources),
            "{print_args_executable}": repr(ctx.executable._print_args_executable.path),
        },
    )
    return DefaultInfo(files = depset([script]))

_expand_template = rule(
    attrs = {
        "labels_to_flags": attr.string_list_dict(mandatory = True),  # string keys instead of label_keyed because Bazel doesn't support parsing wildcard target patterns (..., *, :all) in BUILD attributes.
        "labels_to_groups": attr.string_list_dict(),
        "out_dir": attr.string(default = "."),
        "exclude_external_sources": attr.bool(default = False),
        "exclude_headers": attr.string(values = ["all", "external", ""]),  # "" needed only for compatibility with Bazel < 3.6.0
        "_script_template": attr.label(allow_single_file = True, default = "refresh.template.py"),
        "_print_args_executable": attr.label(executable = True, cfg = "target", default = "//:print_args"),
        # For Windows INCLUDE. If this were eliminated, for example by the resolution of https://github.com/clangd/clangd/issues/123, we'd be able to just use a macro and skylib's expand_template rule: https://github.com/bazelbuild/bazel-skylib/pull/330
        # Once https://github.com/bazelbuild/bazel/pull/17108 is widely released, we should be able to eliminate this and get INCLUDE directly. Perhaps for 7.0? Should be released in the sucessor to 6.0
        "_cc_toolchain": attr.label(default = "@bazel_tools//tools/cpp:current_cc_toolchain"),
    },
    toolchains = ["@bazel_tools//tools/cpp:toolchain_type"],  # Needed for find_cpp_toolchain with --incompatible_enable_cc_toolchain_resolution
    implementation = _expand_template_impl,
)

def _check_python_version_impl(ctx):
    """Sets up check_python_version.template.py to call {to_run}.py's main()"""
    script = ctx.actions.declare_file(ctx.attr.name)
    ctx.actions.expand_template(
        output = script,
        is_executable = True,
        template = ctx.file._template,
        substitutions = {
            "{to_run}": ctx.attr.to_run,
        },
    )
    return DefaultInfo(files = depset([script]))

_check_python_version = rule(
    attrs = {
        "to_run": attr.string(mandatory = True), # Name of the python module (no .py) to import and call .main() on, should checks succeed.
        "_template": attr.label(allow_single_file = True, default = "check_python_version.template.py"),
    },
    implementation = _check_python_version_impl,
)
