"""
As a template, this file helps implement the refresh_compile_commands rule and is not part of the user interface. See ImplementationReadme.md for top-level context -- or refresh_compile_commands.bzl for narrower context.

Interface (after template expansion):
- `bazel run` to regenerate compile_commands.json, so autocomplete (and any other clang tooling!) reflect the latest Bazel build files.
    - No arguments are needed; they're baked into the template expansion.
    - Requires being run under Bazel so we can access the workspace root environment variable.
- Output: a compile_commands.json in the workspace root that clang tooling (or you!) can look at to figure out how files are being compiled by Bazel
    - Crucially, this output is de-Bazeled; The result is a command that could be run from the workspace root directly, with no Bazel-specific requirements, environment variables, etc.
"""

import concurrent.futures
import functools
import itertools
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import types
import typing


# OPTIMNOTE: Most of the runtime of this file--and the output file size--are working around https://github.com/clangd/clangd/issues/123. To work around we have to run clang's preprocessor on files to determine their headers and emit compile commands entries for those headers.
# There is an optimization that would improve speed. We intentionally haven't done it because it has downsides and we anticipate that this problem will be temporary; clangd improves fast. 
    # The simplest would be to only search for headers once per source file.
        # Downside: We could miss headers conditionally included, e.g., by platform.
        # Implementation: skip source files we've already seen in _get_files, shortcutting a bunch of slow preprocessor runs in _get_headers and output. We'd need a threadsafe set, or one set per thread, because header finding is already multithreaded for speed (same magnitudespeed win as single-threaded set).
        # Anticipated speedup: ~2x (30s to 15s.)


def _get_headers(compile_args: typing.List[str], source_path_for_sanity_check: typing.Optional[str] = None):
    """Gets the headers used by a particular compile command.

    Relatively slow. Requires running the C preprocessor.
    """
    # Hacky, but hopefully this is a temporary workaround for the clangd issue mentioned in the caller (https://github.com/clangd/clangd/issues/123)
    # Runs a modified version of the compile command to piggyback on the compiler's preprocessing and header searching.
    # Flags reference here: https://clang.llvm.org/docs/ClangCommandLineReference.html

    # As an alternative approach, you might consider trying to get the headers by inspecing the Middlemen actions in the aquery output, but I don't see a way to get just the ones actually #included--or an easy way to get the system headers--without invoking the preprocessor's header search logic.
        # For more on this, see https://github.com/hedronvision/bazel-compile-commands-extractor/issues/5#issuecomment-1031148373

    # Strip out existing dependency file generation that could interfere with ours.
    # Clang on Apple doesn't let later flags override earlier ones, unfortunately.
    # These flags are prefixed with M for "make", because that's their output format.
    # *-dependencies is the long form. And the output file is traditionally *.d
    header_cmd = (arg for arg in compile_args
        if not arg.startswith('-M') and not arg.endswith(('-dependencies', '.d')))

    # Strip output flags. Apple clang tries to do a full compile if you don't.
    header_cmd = (arg for arg in header_cmd
        if arg != '-o' and not arg.endswith('.o'))

    # Dump system and user headers to stdout...in makefile format, tolerating missing (generated) files
    # Relies on our having made the workspace directory simulate the execroot with //external symlink
    header_cmd = list(header_cmd) + ['--dependencies', '--print-missing-file-dependencies']

    header_search_process = subprocess.run(
        header_cmd,
        cwd=os.environ["BUILD_WORKSPACE_DIRECTORY"],
        capture_output=True,
        encoding='utf-8',
        check=False, # We explicitly ignore errors and carry on.
    )
    headers_makefile_out = header_search_process.stdout

    # Tolerate failure gracefully--during editing the code may not compile!
    print(header_search_process.stderr, file=sys.stderr, end='') # Captured with capture_output and dumped explicitly to avoid interlaced output.
    if not headers_makefile_out: # Worst case, we couldn't get the headers,
        return []
    # But often, we can get the headers, despite the error.

    # Parse the makefile output.
    split = headers_makefile_out.replace('\\\n', '').split() # Undo shell line wrapping bc it's not consistent (depends on file name length)
    assert split[0].endswith('.o:'), "Something went wrong in makefile parsing to get headers. Zeroth entry should be the object file. Output:\n" + headers_makefile_out
    assert source_path_for_sanity_check is None or split[1].endswith(source_path_for_sanity_check), "Something went wrong in makefile parsing to get headers. First entry should be the source file. Output:\n" + headers_makefile_out
    headers = split[2:] # Remove .o and source entries (since they're not headers). Verified above
    headers = list(set(headers)) # Make unique. GCC sometimes emits duplicate entries https://github.com/hedronvision/bazel-compile-commands-extractor/issues/7#issuecomment-975109458

    return headers


def _get_files(compile_args: typing.List[str]):
    """Gets the ([source files], [header files]) clangd should be told the command applies to."""
    source_files = [arg for arg in compile_args if arg.endswith(_get_files.source_extensions)]

    assert len(source_files) > 0, f"No sources detected in {compile_args}"
    assert len(source_files) <= 1, f"Multiple sources detected. Might work, but needs testing, and unlikely to be right given bazel. CMD: {compile_args}"

    # Note: We need to apply commands to headers and sources.
    # Why? clangd currently tries to infer commands for headers using files with similar paths. This often works really poorly for header-only libraries. The commands should instead have been inferred from the source files using those libraries... See https://github.com/clangd/clangd/issues/123 for more.
    # When that issue is resolved, we can stop looking for headers and files can just be the single source file. Good opportunity to clean that out.
    if source_files[0] in _get_files.assembly_source_extensions: # Assembly sources that are not preprocessed can't include headers
        return source_files, []
    header_files = _get_headers(compile_args, source_files[0])

    # Ambiguous .h headers need a language specified if they aren't C, or clangd will erroneously assume they are C
    # Will be resolved by https://reviews.llvm.org/D116167. Revert f24fc5e and test when that lands, presumably in clangd14.
    # See also: https://github.com/hedronvision/bazel-compile-commands-extractor/issues/12
    if (any(header_file.endswith('.h') for header_file in header_files) 
        and not source_files[0].endswith(_get_files.c_source_extensions)
        and all(not arg.startswith('-x') and not arg.startswith('--language') and arg.lower() not in ('-objc', '-objc++') for arg in compile_args)):
        # Insert at front of (non executable) args, because the --language is only supposed to take effect on files listed thereafter
        compile_args.insert(1, _get_files.extensions_to_language_args[os.path.splitext(source_files[0])[1]]) 

    return source_files, header_files
# Setup extensions and flags for the whole C-language family.
_get_files.c_source_extensions = ('.c',)
_get_files.cpp_source_extensions = ('.cc', '.cpp', '.cxx', '.c++', '.C')
_get_files.objc_source_extensions = ('.m',)
_get_files.objcpp_source_extensions = ('.mm',)
_get_files.cuda_source_extensions = ('.cu',)
_get_files.opencl_source_extensions = ('.cl',)
_get_files.assembly_source_extensions = ('.s', '.asm')
_get_files.assembly_needing_c_preprocessor_source_extensions = ('.S',)
_get_files.source_extensions = _get_files.c_source_extensions + _get_files.cpp_source_extensions + _get_files.objc_source_extensions + _get_files.objcpp_source_extensions + _get_files.cuda_source_extensions + _get_files.opencl_source_extensions + _get_files.assembly_source_extensions + _get_files.assembly_needing_c_preprocessor_source_extensions
_get_files.extensions_to_language_args = {
    _get_files.c_source_extensions: '--language=c',
    _get_files.cpp_source_extensions: '--language=c++',
    _get_files.objc_source_extensions: '-ObjC',
    _get_files.objcpp_source_extensions: '-ObjC++',
    _get_files.cuda_source_extensions: '--language=cuda',
    _get_files.opencl_source_extensions: '--language=cl',
    _get_files.assembly_source_extensions: '--language=assembler',
    _get_files.assembly_needing_c_preprocessor_source_extensions: '--language=assembler-with-cpp',
}
_get_files.extensions_to_language_args = {ext : flag for exts, flag in _get_files.extensions_to_language_args.items() for ext in exts} # Flatten map for easier use


@functools.lru_cache(maxsize=None)
def _get_apple_SDKROOT(SDK_name: str):
    """Get path to xcode-select'd root for the given OS."""
    # We're manually building the path because something like `xcodebuild -sdk iphoneos` requires different capitalization and more parsing, and this is a hack anyway until Bazel fixes https://github.com/bazelbuild/bazel/issues/12852
    return f'{_get_apple_DEVELOPER_DIR()}/Platforms/{SDK_name}.platform/Developer/SDKs/{SDK_name}.sdk'
    # Unless xcode-select has been invoked (like for a beta) we'd expect '/Applications/Xcode.app/Contents/Developer/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS.sdk'
    # Traditionally stored in SDKROOT environment variable, but not provided.


def _get_apple_platform(compile_args: typing.List[str]):
    """Figure out which Apple platform a command is for.

    Is the name used by Xcode in the SDK files, not the marketing name.
    e.g. iPhoneOS, not iOS.
    """
    # A bit gross, but Bazel specifies the platform name in one of the include paths, so we mine it from there.
    for arg in compile_args:
        match = re.search('/Platforms/([a-zA-Z]+).platform/Developer/', arg)
        if match:
            return match.group(1)
    return None


@functools.lru_cache(maxsize=None)
def _get_apple_DEVELOPER_DIR():
    """Get path to xcode-select'd developer directory."""
    return subprocess.check_output(('xcode-select', '--print-path'), encoding='utf-8').rstrip()
    # Unless xcode-select has been invoked (like for a beta) we'd expect '/Applications/Xcode.app/Contents/Developer' from xcode-select -p
    # Traditionally stored in DEVELOPER_DIR environment variable, but not provided.


@functools.lru_cache(maxsize=None)
def _get_apple_active_clang():
    """Get path to xcode-select'd clang version."""
    return subprocess.check_output(('xcrun', '--find', 'clang'), encoding='utf-8').rstrip()
    # Unless xcode-select has been invoked (like for a beta) we'd expect '/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang' from xcrun -f clang


def _apple_platform_patch(compile_args: typing.List[str]):
    """De-Bazel the command into something clangd can parse.

    This function has fixes specific to Apple platforms, but you should call it on all platforms. It'll determine whether the fixes should be applied or not.
    """
    compile_args = list(compile_args)
    # Bazel internal environment variable fragment that distinguishes Apple platforms that need unwrapping.
        # Note that this occurs in the Xcode-installed wrapper, but not the CommandLineTools wrapper, which works fine as is. 
    if any('__BAZEL_XCODE_' in arg for arg in compile_args):
        # Undo Bazel's Apple platform compiler wrapping.
        # Bazel wraps the compiler as `external/local_config_cc/wrapped_clang` and exports that wrapped compiler in the proto, and we need a clang call that clangd can introspect. (See notes in "how clangd uses compile_commands.json" in ImplementationReadme.md for more.)
        # It's also important because Bazel's Xcode (but not CommandLineTools) wrapper crashes if you don't specify particular environment variables (replaced below).
        # When https://github.com/clangd/clangd/issues/123 is resolved, we might be able to remove this line without causing crashes or missing standard library or system framework red squigglies, since clangd is able to work correctly through other wrappers, like the CommandLineTools wrapper or the llvm wrappers. But currently, it's critical for being able to invoking the command to get headers without depending on environment variables. Still, it probably makes sense to leave it so the commands in compile_commands.json are invokable independent of Bazel. 
        compile_args[0] = _get_apple_active_clang()

        # We have to manually substitute out Bazel's macros so clang can parse the command
        # Code this mirrors is in https://github.com/bazelbuild/bazel/blob/master/tools/osx/crosstool/wrapped_clang.cc
        # Not complete--we're just swapping out the essentials, because there seems to be considerable turnover in the hacks they have in the wrapper.
        compile_args = [arg.replace('DEBUG_PREFIX_MAP_PWD', "-fdebug-prefix-map="+os.getcwd()) for arg in compile_args]
        # We also have to manually figure out the values of SDKROOT and DEVELOPER_DIR, since they're missing from the environment variables Bazel provides.
        # Filed Bazel issue about the missing environment variables: https://github.com/bazelbuild/bazel/issues/12852
        compile_args = [arg.replace('__BAZEL_XCODE_DEVELOPER_DIR__', _get_apple_DEVELOPER_DIR()) for arg in compile_args]
        apple_platform = _get_apple_platform(compile_args)
        assert apple_platform, f"Apple platform not detected in CMD: {compile_args}"
        compile_args = [arg.replace('__BAZEL_XCODE_SDKROOT__', _get_apple_SDKROOT(apple_platform)) for arg in compile_args]

    return compile_args


def _all_platform_patch(compile_args: typing.List[str]):
    """Apply de-Bazeling fixes to the compile command that are shared across target platforms."""
    # clangd writes module cache files to the wrong place
    # Without this fix, you get tons of module caches dumped into the VSCode root folder.
    # Filed clangd issue at: https://github.com/clangd/clangd/issues/655
    # Seems to have disappeared when we switched to aquery from action_listeners, but we'll leave it in until the bug is patched in case we start using C++ modules
    compile_args = (arg for arg in compile_args if not arg.startswith('-fmodules-cache-path=bazel-out/'))

    # When Bazel builds with gcc it adds -fno-canonical-system-headers to the command line, which clang tooling chokes on, since it does not understand this flag.
    # We'll remove this flag, until such time as clangd & clang-tidy gracefully ignore it. Tracking issue: https://github.com/clangd/clangd/issues/1004.
    # For more context see: https://github.com/hedronvision/bazel-compile-commands-extractor/issues/21
    compile_args = (arg for arg in compile_args if not arg == '-fno-canonical-system-headers')

    # Any other general fixes would go here...

    return list(compile_args)


def _get_cpp_command_for_files(compile_action):
    """Reformat compile_action into a command clangd can understand.

    Undo Bazel-isms and figures out which files clangd should apply the command to.
    """
    args = compile_action.arguments

    # Patch command by platform
    args = _all_platform_patch(args)
    args = _apple_platform_patch(args)
    # Android and Linux and grailbio LLVM toolchains: Fine as is; no special patching needed.

    source_files, header_files = _get_files(args)
    return source_files, header_files, args


def _extract(aquery_output):
    """Converts from Bazel's aquery format to de-Bazeled compile_commands.json entries.

    Input: jsonproto output from aquery, pre-filtered to (Objective-)C(++) compile actions for a given build.
    Yields: Corresponding entries for a compile_commands.json, with commas after each entry, describing all ways every file is being compiled.
        Also includes one entry per header, describing one way it is compiled (to work around https://github.com/clangd/clangd/issues/123).

    Crucially, this de-Bazels the compile commands it takes as input, leaving something clangd can understand. The result is a command that could be run from the workspace root directly, with no bazel-specific environment variables, etc.
    """

    # Process each action from Bazelisms -> file paths and their clang commands
    # Threads instead of processes because most of the execution time is farmed out to subprocesses. No need to sidestep the GIL. Might change after https://github.com/clangd/clangd/issues/123 resolved
    with concurrent.futures.ThreadPoolExecutor(
        # Default since Python 3.8, but let's make that explicit to avoid
        # "using very large resources implicitly on many-core machines" with
        # Python 3.7.
        # https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.ThreadPoolExecutor
        max_workers=min(32, (os.cpu_count() or 1) + 4)
    ) as threadpool:
        outputs = threadpool.map(_get_cpp_command_for_files, aquery_output.actions)

    # Yield as compile_commands.json entries
    header_file_entries_written = set()
    for source_files, header_files, args in outputs:
        # Only emit one entry per header
        # This makes the output vastly smaller, which has been a problem for users.
        # e.g. https://github.com/insufficiently-caffeinated/caffeine/pull/577
        # Without this, we emit an entry for each header for each time it is included, which is explosively duplicative--the same reason why C++ compilation is slow and the impetus for the new modules.
        # Revert when https://github.com/clangd/clangd/issues/123 is solved, which would remove the need to emit headers, because clangd would take on that work.
        # If https://github.com/clangd/clangd/issues/681, we'd probably want to find a way to filter to one entry per platform.
        header_files = [h for h in header_files if h not in header_file_entries_written]
        header_file_entries_written.update(header_files)

        for file in itertools.chain(source_files, header_files):
            yield {
                "file": file,
                "arguments": args,
                # Bazel gotcha warning: If you were tempted to use `bazel info execution_root` as the build working directory for compile_commands...search ImplementationReadme.md to learn why that breaks.
                "directory": os.environ["BUILD_WORKSPACE_DIRECTORY"],
            }


def _get_commands(target: str, flags: str):
    """Yields compile_commands.json entries for a given target and flags, gracefully tolerating errors."""
    # Log clear completion messages
    print(f"\033[0;34m>>> Analyzing commands used in {target}\033[0m", file=sys.stderr)

    # First, query Bazel's C-family compile actions for that configured target
    cmd = [
        "bazel",
        "aquery",
        # Aquery docs if you need em: https://docs.bazel.build/versions/master/aquery.html
        # One bummer, not described in the docs, is that aquery filters over *all* actions for a given target, rather than just those that would be run by a build to produce a given output. This mostly isn't a problem, but can sometimes surface extra, unnecessary, misconfigured actions. Chris has emailed the authors to discuss and filed an issue so anyone reading this could track it: https://github.com/bazelbuild/bazel/issues/14156.
        f"mnemonic('(Objc|Cpp)Compile',deps({target}))",
        # We switched to jsonproto instead of proto because of https://github.com/bazelbuild/bazel/issues/13404. We could change back when fixed--reverting most of the commit that added this line and tweaking the build file to depend on the target in that issue. That said, it's kinda nice to be free of the dependency, unless (OPTIMNOTE) jsonproto becomes a performance bottleneck compated to binary protos.
        "--output=jsonproto",
        # Shush logging. Just for readability.
        "--ui_event_filters=-info",
        "--noshow_progress",
    ] + shlex.split(flags)

    aquery_process = subprocess.run(
        cmd,
        cwd=os.environ["BUILD_WORKSPACE_DIRECTORY"],
        capture_output=True,
        encoding='utf-8',
        check=False, # We explicitly ignore errors from `bazel aquery` and carry on.
    )

    # Filter aquery error messages to just those the user should care about.
    for line in aquery_process.stderr.splitlines():
        # Shush known warnings about missing graph targets.
        # The missing graph targets are not things we want to introspect anyway.
        # Tracking issue https://github.com/bazelbuild/bazel/issues/13007.
        if line.startswith("WARNING: Targets were missing from graph:"):
            continue

        print(line, file=sys.stderr)

    try:
        # object_hook allows object.member syntax, just like a proto, while avoiding the protobuf dependency
        parsed_aquery_output = json.loads(aquery_process.stdout, object_hook=lambda d: types.SimpleNamespace(**d))
    except json.JSONDecodeError:
        print("aquery failed. Command:", " ".join(shlex.quote(arg) for arg in cmd), file=sys.stderr)
        print(f"\033[0;32m>>> Failed extracting commands for {target}\n    Continuing gracefully...\033[0m",  file=sys.stderr)
        return

    # Load aquery's output from the proto data being piped to stdin
    # Proto reference: https://github.com/bazelbuild/bazel/blob/master/src/main/protobuf/analysis_v2.proto
    yield from _extract(parsed_aquery_output)

    # Log clear completion messages
    print(f"\033[0;32m>>> Finished extracting commands for {target}\033[0m", file=sys.stderr)


if __name__ == "__main__":
    target_flag_pairs = [
        # Begin: Command template filled by Bazel
        {target_flag_pairs}
        # End: Command template filled by Bazel
    ]
    compile_command_entries = []
    for (target, flags) in target_flag_pairs:
        compile_command_entries.extend(_get_commands(target, flags))

    # Chain output into compile_commands.json
    workspace_root = pathlib.Path(os.environ["BUILD_WORKSPACE_DIRECTORY"]) # Set by `bazel run`
    with open(workspace_root / "compile_commands.json", "w") as output_file:
        json.dump(
            compile_command_entries,
            output_file, 
            indent=2, # Yay, human readability!
            check_circular=False # For speed.
        )
