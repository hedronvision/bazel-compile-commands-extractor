"""Extract the compilation commands from a bazel aquery.

This is CompileCommands implementation, not interface. See README.md for documentation of the tool's user interface.

Meant to be run via Bazel by refresh.sh, not directly. See invocation there.
Requires being run under Bazel.

Input (stdin): jsonproto output from aquery, pre-filtered to (Objective-)C(++) compile actions for a given build.
Output (stdout): Corresponding entries for a compile_commands.json, with commas after each entry, describing all ways every file is being compiled.

Crucially, this de-Bazels the compile commands it takes as input, leaving something clangd can understand. The result is a command that could be run from the workspace root directly, with no bazel-specific compiler wrappers, environment variables, etc.
"""


import concurrent.futures
import functools
import json
import os
import re
import subprocess
import sys
from types import SimpleNamespace
from typing import Optional, List


# OPTIMNOTE: Most of the runtime of this file--and the output file size--are working around https://github.com/clangd/clangd/issues/519. To workaround we have to run clang's preprocessor on files to determine their headers and emit compile commands entries for those headers.
# There are some optimizations that would improve speed and file size but we intentionally haven't done them because we anticipate that this problem will be temporary; clangd improves fast. 
    # The simplest would be to only emit one entry per file--omitting an incomplete compilation database but one that's smaller and good enough for clangd.
        # ...by skipping source and header files we've already seen in _get_files, shortcutting a bunch of slow preprocessor runs in _get_headers and output. We'd need a threadsafe set, because header finding is already multithreaded for speed (same speed win as single-threaded set).
        # Anticipated speedup: ~2x (30s to 15s.) File size reduced about 22x (350MB to 16MB).
        # No user impact because clangd currently just picks one command per file arbitrarily, but that will change if they do better multiplatform compilation (see https://github.com/clangd/clangd/issues/681). And if they implement multiplatform compilation, then we wouldn't want to omit subsequent compilations of the same file.


def _get_headers(compile_args: List[str], source_path_for_sanity_check: Optional[str] = None):
    """Gets the headers used by a particular compile command.

    Relatively slow. Requires running the C preprocessor.
    """
    # Hacky, but hopefully this is a temporary workaround for the clangd issue mentioned in the caller (https://github.com/clangd/clangd/issues/519)
    # Runs a modified version of the compile command to piggyback on the compiler's preprocessing and header searching.
    _check_in_clang_args_format(compile_args) # Assuming clang/gcc flag format.
    # Flags reference here: https://clang.llvm.org/docs/ClangCommandLineReference.html

    # Strip out existing dependency file generation that could interfere with ours
    # Clang on Apple doesn't let later flags override earlier ones, unfortunately
    # These flags are prefixed with M for "make", because that's their output format.
    # *-dependencies is the long form. And the output file is traditionally *.d
    header_cmd = (arg for arg in compile_args
        if not arg.startswith('-M') and not arg.endswith(('-dependencies', '.d')))

    # Strip output flags. Apple clang tries to do a full compile if you don't.
    header_cmd = (arg for arg in header_cmd
        if arg != '-o' and not arg.endswith('.o'))

    # Dump system and user headers to stdout...in makefile format, tolerating missing (generated) files
    header_cmd = list(header_cmd) + ['--dependencies', '--print-missing-file-dependencies']

    try:
        headers_makefile_out = subprocess.check_output(header_cmd, encoding='utf-8', cwd=os.environ['BUILD_WORKSPACE_DIRECTORY']).rstrip() # Relies on our having made the workspace directory simulate the execroot with //external symlink
    except subprocess.CalledProcessError as e:
        # Tolerate failure gracefully--during editing the code may not compile!
        if not e.output: # Worst case, we couldn't get the headers
            return []
        headers_makefile_out = e.output # But often, we can get the headers, despite the error

    split = headers_makefile_out.replace('\\\n', '').split() # Undo shell line wrapping bc it's not consistent (depends on file name length)
    assert split[0].endswith('.o:'), "Something went wrong in makefile parsing to get headers. Output:\n" + headers_makefile_out
    assert source_path_for_sanity_check is None or split[1].endswith(source_path_for_sanity_check), "Something went wrong in makefile parsing to get headers. Output:\n" + headers_makefile_out

    headers = [h.strip() for h in split[2:]]
    assert len(headers) == len(set(headers)), "Compiler should have ensured uniqueness of header files"

    return headers


def _get_files(compile_args: List[str]):
    """Gets all source files clangd should be told the command applies to."""
    # Whole C-language family.
    source_extensions = ('.c', '.cc', '.cpp', '.cxx', '.c++', '.C', '.m', '.mm', '.M')
    source_files = [arg for arg in compile_args if arg.endswith(source_extensions)]

    assert len(source_files) > 0, f"No sources detected in {compile_args}"
    assert len(source_files) <= 1, f"Multiple sources detected. Might work, but needs testing, and unlikely to be right given bazel. CMD: {compile_args}"

    # Note: We need to apply commands to headers and sources.
    # Why? clangd currently tries to infer commands for headers using files with similar paths. This often works really poorly for header-only libraries. The commands should instead have been inferred from the source files using those libraries... See https://github.com/clangd/clangd/issues/519 for more.
    # When that issue is resolved, we can stop looking for headers and files can just be the single source file. Good opportunity to clean that out.
    files = source_files + _get_headers(compile_args, source_files[0])

    return files


def _check_in_clang_args_format(compile_args: List[str]):
    # Just sharing an assert we use twice. When https://github.com/clangd/clangd/issues/519 is resolved, we can fold this into the single caller.
    # Quickly just check that the compiler looks like clang.
    # Really clang is mimicing gcc for compatibility, but clang is so dominant these days, that we'll name the function this way.
    assert compile_args[0].endswith(('clang', 'clang++', 'gcc')), f"Compiler doesn't look like normal clang/gcc. Time to add windows support? CMD: {compile_args}"


def _all_platform_patch(compile_args: List[str]):
    """Apply de-Bazeling fixes to the compile command that are shared across target platforms."""
    # clangd writes module cache files to the wrong place
    # Without this fix, you get tons of module caches dumped into the VSCode root folder.
    # Filed clangd issue at: https://github.com/clangd/clangd/issues/655
    # Seems to have disappeared when we switched to aquery from action_listeners, but we'll leave it in until the bug is patched in case we start using C++ modules
    compile_args = (arg for arg in compile_args if not arg.startswith('-fmodules-cache-path=bazel-out/'))

    # Any other general fixes would go here...

    return list(compile_args)


@functools.lru_cache(maxsize=None)
def _get_apple_SDKROOT(SDK_name: str):
    """Get path to xcode-select'd root for the given OS."""
    # We're manually building the path because something like `xcodebuild -sdk iphoneos` requires different capitalization and more parsing, and this is a hack anyway until Bazel fixes https://github.com/bazelbuild/bazel/issues/12852
    return f'{_get_apple_DEVELOPER_DIR()}/Platforms/{SDK_name}.platform/Developer/SDKs/{SDK_name}.sdk'
    # Unless xcode-select has been invoked (like for a beta) we'd expect '/Applications/Xcode.app/Contents/Developer/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS.sdk'
    # Traditionally stored in SDKROOT environment variable, but not provided.


def _get_apple_platform(compile_args: List[str]):
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


def _apple_platform_patch(compile_args: List[str]):
    """De-Bazel the command into something clangd can parse.

    This function has fixes specific to Apple platforms, but you should call it on all platforms. It'll determine whether the fixes should be applied or not.
    """
    compile_args = list(compile_args)
    if any('__BAZEL_XCODE_' in arg for arg in compile_args): # Bazel internal environment variable fragment that distinguishes Apple platforms
        # Undo Bazel's compiler wrapping.
        # Bazel wraps the compiler as `external/local_config_cc/wrapped_clang` and exports that wrapped compiler in the proto, and we need a clang call that clangd can introspect. (See notes in "how clangd uses compile_commands.json" in ImplementationReadme.md for more.)
        compile_args[0] = _get_apple_active_clang()

        # We have to manually substitute out Bazel's macros so clang can parse the command
        # Code this mirrors is in https://github.com/keith/bazel/blob/master/tools/osx/crosstool/wrapped_clang.cc
        # Not complete--we're just swapping out the essentials, because there seems to be considerable turnover in the hacks they have in the wrapper.
        compile_args = [arg.replace('DEBUG_PREFIX_MAP_PWD', "-fdebug-prefix-map="+os.getcwd()) for arg in compile_args]
        # We also have to manually figure out the values of SDKROOT and DEVELOPER_DIR, since they're missing from the environment variables Bazel provides.
        # Filed Bazel issue about the missing environment variables: https://github.com/bazelbuild/bazel/issues/12852
        compile_args = [arg.replace('__BAZEL_XCODE_DEVELOPER_DIR__', _get_apple_DEVELOPER_DIR()) for arg in compile_args]
        apple_platform = _get_apple_platform(compile_args)
        assert apple_platform, f"Apple platform not detected in CMD: {compile_args}"
        compile_args = [arg.replace('__BAZEL_XCODE_SDKROOT__', _get_apple_SDKROOT(apple_platform)) for arg in compile_args]

    return compile_args


def _get_cpp_command_for_files(compile_action: json):
    """Reformat compile_action into a command clangd can understand.

    Undo Bazel-isms and figures out which files clangd should apply the command to.
    """
    args = compile_action.arguments

    # Patch command by platform
    args = _all_platform_patch(args)
    args = _apple_platform_patch(args)
    # Android: Fine as is; no special patching needed.

    _check_in_clang_args_format(args) # Sanity check

    files = _get_files(args)
    command = ' '.join(args) # Reformat options as command string
    return files, command


if __name__ == '__main__':
    # Load aquery's output from the proto data being piped to stdin
    # Proto reference: https://github.com/bazelbuild/bazel/blob/master/src/main/protobuf/analysis_v2.proto
    aquery_output = json.loads(sys.stdin.buffer.read(), object_hook=lambda d: SimpleNamespace(**d)) # object_hook allows object.member syntax, just like a proto, while avoiding the protobuf dependency

    # Process each action from Bazelisms -> file paths and their clang commands
    with concurrent.futures.ThreadPoolExecutor() as threadpool:
        outputs = threadpool.map(_get_cpp_command_for_files, aquery_output.actions)

    bazel_workspace_dir = os.environ['BUILD_WORKSPACE_DIRECTORY'] # Set by `bazel run`. Can't call `bazel info workspace` because bazel is running us outside the workspace.
    # Bazel gotcha warning: If you were tempted to use `bazel info execution_root` as the build working directory for compile_commands...search ImplementationReadme.md to learn why that breaks.

    # Dump em to stdout as compile_commands.json entries
    for files, command in outputs:
        for file in files:
            sys.stdout.write(json.dumps({
                'file': file,
                'command': command,
                'directory': bazel_workspace_dir
            }, indent=2, check_circular=False))
            sys.stdout.write(',')
