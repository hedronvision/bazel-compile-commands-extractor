"""
As a template, this file helps implement the refresh_compile_commands rule and is not part of the user interface. See ImplementationReadme.md for top-level context -- or refresh_compile_commands.bzl for narrower context.

Interface (after template expansion):
- `bazel run` to regenerate compile_commands.json, so autocomplete (and any other clang tooling!) reflect the latest Bazel build files.
    - No arguments are needed; info from the rule baked into the template expansion.
        - Any arguments passed are interpreted as arguments needed for the builds being analyzed.
    - Requires being run under Bazel so we can access the workspace root environment variable.
- Output: a compile_commands.json in the workspace root that clang tooling (or you!) can look at to figure out how files are being compiled by Bazel
    - Crucially, this output is de-Bazeled; The result is a command that could be run from the workspace root directly, with no Bazel-specific requirements, environment variables, etc.
"""

import sys
if sys.version_info < (3,7):
    sys.exit("\n\033[31mFATAL ERROR:\033[0m Python 3.7 or later is required. Please update!")
    # 3.7 backwards compatibility required by @lummax in https://github.com/hedronvision/bazel-compile-commands-extractor/pull/27. Try to contact him before upgrading.
    # When adding things could be cleaner if we had a higher minimum version, please add a comment with MIN_PY=3.<v>.
    # Similarly, when upgrading, please search for that MIN_PY= tag.


import concurrent.futures
import functools
import itertools
import json
import locale
import os
import pathlib
import re
import shlex
import subprocess
import types
import typing # MIN_PY=3.9: Switch e.g. typing.List[str] -> list[str]


# OPTIMNOTE: Most of the runtime of this file--and the output file size--are working around https://github.com/clangd/clangd/issues/123. To work around we have to run clang's preprocessor on files to determine their headers and emit compile commands entries for those headers.
# There are some optimization that would improve speed. Here are the ones we've thought of in case we ever want them. But we we anticipate that this problem will be temporary; clangd improves fast.
    # The simplest would be to only search for headers once per source file.
        # Downside: We could miss headers conditionally included, e.g., by platform.
        # Implementation: skip source files we've already seen in _get_files, shortcutting a bunch of slow preprocessor runs in _get_headers and output. We'd need a threadsafe set, or one set per thread, because header finding is already multithreaded for speed (same magnitudespeed win as single-threaded set).
        # Anticipated speedup: ~2x (30s to 15s.)
    # A better one would be to cache include information. 
        # We could check to see if Bazel has cached .d files listing the dependencies and use those instead of preprocessing everything to regenerate them.
            # If all the files listed in the .d file have older last-modified dates than the .d file itself, this should be safe. We'd want to check that bazel isn't 0-timestamping generated files, though.
        # We could also write .d files when needed, saving work between runs.
        # Maybe there's a good way of doing the equivalent on Windows, too, maybe using /sourceDependencies


def _print_header_finding_warning_once():
    """Gives users context about "compiler errors" while header finding. Namely that we're recovering."""
    # Shared between platforms

    # Just log once; subsequent messages wouldn't add anything.
    if _print_header_finding_warning_once.has_logged: return 
    _print_header_finding_warning_once.has_logged = True

    print("""\033[0;33m>>> While locating the headers you use, we encountered a compiler warning or error.
    No need to worry; your code doesn't have to compile for this tool to work.
    However, we'll still print the errors and warnings in case they're helpful for you in fixing them.
    If the errors are about missing files Bazel should generate:
        You might want to run a build of your code with --keep_going.
        That way, everything possible is generated, browsable and indexed for autocomplete.
    But, if you have *already* built your code successfully:
        Please make sure you're supplying this tool with the same flags you use to build.
        You can either use a refresh_compile_commands rule or the special -- syntax. Please see the README.
        [Supplying flags normally won't work. That just causes this tool to be built with those flags.]
    Continuing gracefully...\033[0m""",  file=sys.stderr)
_print_header_finding_warning_once.has_logged = False


def _get_headers_gcc(compile_args: typing.List[str], source_path_for_sanity_check: typing.Optional[str] = None):
    """Gets the headers used by a particular compile command that uses gcc arguments formatting (including clang.)

    Relatively slow. Requires running the C preprocessor.
    """
    # Flags reference here: https://clang.llvm.org/docs/ClangCommandLineReference.html

    # Strip out existing dependency file generation that could interfere with ours.
    # Clang on Apple doesn't let later flags override earlier ones, unfortunately.
    # These flags are prefixed with M for "make", because that's their output format.
    # *-dependencies is the long form. And the output file is traditionally *.d
    header_cmd = (arg for arg in compile_args
        if not arg.startswith('-M') and not arg.endswith(('-dependencies', '.d')))

    # Strip output flags. Apple clang tries to do a full compile if you don't.
    header_cmd = (arg for arg in header_cmd
        if arg != '-o' and not arg.endswith('.o'))

    # Strip sanitizer ignore lists...so they don't show up in the dependency list.
    # See https://clang.llvm.org/docs/SanitizerSpecialCaseList.html and https://github.com/hedronvision/bazel-compile-commands-extractor/issues/34 for more context.
    header_cmd = (arg for arg in header_cmd
        if not arg.startswith('-fsanitize'))

    # Dump system and user headers to stdout...in makefile format, tolerating missing (generated) files
    # Relies on our having made the workspace directory simulate a complete version of the execroot with //external symlink
    header_cmd = list(header_cmd) + ['--dependencies', '--print-missing-file-dependencies']

    header_search_process = subprocess.run(
        header_cmd,
        capture_output=True,
        encoding=locale.getpreferredencoding(),
        check=False, # We explicitly ignore errors and carry on.
    )
    headers_makefile_out = header_search_process.stdout

    # Tolerate failure gracefully--during editing the code may not compile!
    if header_search_process.stderr:
        _print_header_finding_warning_once()
        print(header_search_process.stderr, file=sys.stderr, end='') # Captured with capture_output and dumped explicitly to avoid interlaced output.

    if not headers_makefile_out: # Worst case, we couldn't get the headers,
        return set()
    # But often, we can get the headers, despite the error.

    # Parse the makefile output.
    split = headers_makefile_out.replace('\\\n', '').split() # Undo shell line wrapping bc it's not consistent (depends on file name length)
    assert split[0].endswith('.o:'), "Something went wrong in makefile parsing to get headers. Zeroth entry should be the object file. Output:\n" + headers_makefile_out
    assert source_path_for_sanity_check is None or split[1].endswith(source_path_for_sanity_check), "Something went wrong in makefile parsing to get headers. First entry should be the source file. Output:\n" + headers_makefile_out
    headers = split[2:] # Remove .o and source entries (since they're not headers). Verified above
    headers = set(headers) # Make unique. GCC sometimes emits duplicate entries https://github.com/hedronvision/bazel-compile-commands-extractor/issues/7#issuecomment-975109458

    return headers


def _get_headers_msvc(compile_args: typing.List[str], source_path: str):
    """Gets the headers used by a particular compile command that uses msvc argument formatting (including clang-cl.)

    Relatively slow. Requires running the C preprocessor.
    """
    # Flags reference here: https://docs.microsoft.com/en-us/cpp/build/reference/compiler-options
    # Relies on our having made the workspace directory simulate a complete version of the execroot with //external junction

    header_cmd = list(compile_args) + [
        '/showIncludes', # Print included headers to stderr. https://docs.microsoft.com/en-us/cpp/build/reference/showincludes-list-include-files
        '/EP', # Preprocess (only, no compilation for speed), writing to stdout where we can easily ignore it instead of a file. https://docs.microsoft.com/en-us/cpp/build/reference/ep-preprocess-to-stdout-without-hash-line-directives
    ]

    # cl.exe needs the `INCLUDE` environment variable to find the system headers, since they aren't specified in the action command
    # Bazel neglects to include INCLUDE per action, so we'll do the best we can and infer them from the default (host) cc toolchain.
        # These are set in https://github.com/bazelbuild/bazel/bloc/master/tools/cpp/windows_cc_configure.bzl. Search INCLUDE.
        # Bazel should have supplied the environment variables in aquery output but doesn't https://github.com/bazelbuild/bazel/issues/12852
    # Non-Bazel Windows users would normally configure these by calling vcvars
        # For more, see https://docs.microsoft.com/en-us/cpp/build/building-on-the-command-line
    environment = dict(os.environ)
    environment['INCLUDE'] = os.pathsep.join((
        # Begin: template filled by Bazel
        {windows_default_include_paths}
        # End:   template filled by Bazel
    ))

    header_search_process = subprocess.run(
        header_cmd,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        env=environment,
        encoding=locale.getpreferredencoding(),
        check=False, # We explicitly ignore errors and carry on.
    )

    # Based on the locale, `cl.exe` will emit different marker strings. See also https://github.com/ninja-build/ninja/issues/613#issuecomment-885185024 and https://github.com/bazelbuild/bazel/pull/7966.
    # We can't just set environment['VSLANG'] = "1033" (English) and be done with it, because we can't assume the user has the English language pack installed.
    include_marker = (
        'Note: including file:', # English - United States
        '注意: 包含文件: ', # Chinese - People's Republic of China
        '注意: 包含檔案:', # Chinese - Taiwan
        'Poznámka: Včetně souboru:', # Czech
        'Hinweis: Einlesen der Datei:', # German - Germany
        'Remarque : inclusion du fichier : ', # French - France
        'Nota: file incluso ', # Italian - Italy
        'メモ: インクルード ファイル: ', # Japanese
        '참고: 포함 파일:', # Korean
        'Uwaga: w tym pliku: ', # Polish
        'Observação: incluindo arquivo:', # Portuguese - Brazil
        'Примечание: включение файла: ', # Russian
        'Not: eklenen dosya: ', # Turkish
        'Nota: inclusión del archivo:', # Spanish - Spain (Modern Sort)
    )

    headers = set() # Make unique. MSVC emits duplicate entries.
    error_lines = []
    for line in header_search_process.stderr.splitlines():
        # Gobble up the header inclusion information...
        if source_path.endswith('/' + line) or source_path == line: # Munching the source fileneame echoed the first part of the include output
            continue
        for marker in include_marker:
            if line.startswith(marker):
                headers.add(line[len(marker):].strip())
                break
        else:
            error_lines.append(line)
    if error_lines: # Output all errors at the end so they aren't interlaced due to concurrency
        _print_header_finding_warning_once()
        print('\n'.join(error_lines), file=sys.stderr)

    return headers


def _get_headers(compile_args: typing.List[str], source_path: str):
    """Gets the headers used by a particular compile command.

    Relatively slow. Requires running the C preprocessor.
    """
    # Hacky, but hopefully this is a temporary workaround for the clangd issue mentioned in the caller (https://github.com/clangd/clangd/issues/123)
    # Runs a modified version of the compile command to piggyback on the compiler's preprocessing and header searching.

    # As an alternative approach, you might consider trying to get the headers by inspecing the Middlemen actions in the aquery output, but I don't see a way to get just the ones actually #included--or an easy way to get the system headers--without invoking the preprocessor's header search logic.
        # For more on this, see https://github.com/hedronvision/bazel-compile-commands-extractor/issues/5#issuecomment-1031148373

    # Rather than print a scary compiler error, warn gently 
    if not os.path.isfile(source_path):
        if not _get_headers.has_logged_missing_file_error: # Just log once; subsequent messages wouldn't add anything.
            _get_headers.has_logged_missing_file_error = True
            print(f"""\033[0;33m>>> A source file you compile doesn't (yet) exist: {source_path}
    It's probably a generated file, and you haven't yet run a build to generate it.
    That's OK; your code doesn't even have to compile for this tool to work.
    If you can, though, you might want to run a build of your code.
        That way everything is generated, browsable and indexed for autocomplete.
    However, if you have *already* built your code, and generated the missing file...
        Please make sure you're supplying this tool with the same flags you use to build.
        You can either use a refresh_compile_commands rule or the special -- syntax. Please see the README. 
        [Supplying flags normally won't work. That just causes this tool to be built with those flags.]
    Continuing gracefully...\033[0m""",  file=sys.stderr)
        return set()

    if compile_args[0].endswith('cl.exe'): # cl.exe and also clang-cl.exe
        return _get_headers_msvc(compile_args, source_path)
    return _get_headers_gcc(compile_args, source_path)
_get_headers.has_logged_missing_file_error = False


def _get_files(compile_args: typing.List[str]):
    """Gets the ({source files}, {header files}) clangd should be told the command applies to."""
    # Bazel puts the source file being compiled after the -c flag, so we look for the source file there.
    # This is a strong assumption about Bazel internals, so we're taking special care to check that this condition holds with asserts. That way things don't fail silently if it changes some day.
        # -c just means compile-only; don't link into a binary. You can definitely have a proper invocation to clang/gcc where the source isn't right after -c, or where -c isn't present at all.
        # However, parsing the command line this way is our best simple option. The other alternatives seem worse:
            # You can't just filter the args to those that end with source-file extensions. The problem is that sometimes header search directories have source-file extensions. Horrible, but unfortunately true.
                # Parsing the clang invocation properly to get the positional file arguments is hard and not future-proof if new flags are added. Consider a new flag -foo. Does it also capture the next argument after it?
            # You might be tempted to crawl the inputs depset in the aquery output structure, but it's a fair amount of recursive code and there are other erroneous source files there, at least when building for Android in Bazel 5.1. You could fix this by intersecting the set of source files in the inputs with those listed as arguments on the command line, but I can imagine perverse, problematic cases here. It's a lot more code to still have those caveats.
            # You might be tempted to get the source files out of the action message listed (just) in  aquery --output=text  output, but the message differs for external workspaces and tools. Plus paths with spaces are going to be hard because it's space delimited. You'd have to make even stronger assumptions than the -c.
                # Concretely, the message usually has the form "action 'Compiling foo.cpp'"" -> foo.cpp. But it also has "action 'Compiling src/tools/launcher/dummy.cc [for tool]'" -> external/bazel_tools/src/tools/launcher/dummy.cc
                # If we did ever go this route, you can join the output from aquery --output=text and --output=jsonproto by actionKey.
            # For more context on options and how this came to be, see https://github.com/hedronvision/bazel-compile-commands-extractor/pull/37
    compile_only_flag = '/c' if '/c' in compile_args else '-c' # For Windows/msvc support
    source_index = compile_args.index(compile_only_flag) + 1
    source_file = compile_args[source_index]
    SOURCE_EXTENSIONS = ('.c', '.cc', '.cpp', '.cxx', '.c++', '.C', '.m', '.mm', '.cu', '.cl', '.s', '.asm', '.S')
    assert source_file.endswith(SOURCE_EXTENSIONS), f"Source file not found after {compile_only_flag} in {compile_args}"
    assert source_index + 1 == len(compile_args) or compile_args[source_index + 1].startswith('-') or not compile_args[source_index + 1].endswith(SOURCE_EXTENSIONS), f"Multiple sources detected after {compile_only_flag}. Might work, but needs testing, and unlikely to be right given Bazel's incremental compilation. CMD: {compile_args}"

    # Note: We need to apply commands to headers and sources.
    # Why? clangd currently tries to infer commands for headers using files with similar paths. This often works really poorly for header-only libraries. The commands should instead have been inferred from the source files using those libraries... See https://github.com/clangd/clangd/issues/123 for more.
    # When that issue is resolved, we can stop looking for headers and just return the single source file.
    return {source_file}, _get_headers(compile_args, source_file)


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
    return subprocess.check_output(('xcode-select', '--print-path'), encoding=locale.getpreferredencoding()).rstrip()
    # Unless xcode-select has been invoked (like for a beta) we'd expect '/Applications/Xcode.app/Contents/Developer' from xcode-select -p
    # Traditionally stored in DEVELOPER_DIR environment variable, but not provided.


@functools.lru_cache(maxsize=None)
def _get_apple_active_clang():
    """Get path to xcode-select'd clang version."""
    return subprocess.check_output(('xcrun', '--find', 'clang'), encoding=locale.getpreferredencoding()).rstrip()
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
        # Bazel wraps the compiler as `external/local_config_cc/wrapped_clang` and exports that wrapped compiler in the proto. However, we need a clang call that clangd can introspect. (See notes in "how clangd uses compile_commands.json" in ImplementationReadme.md for more.)
        # Removing the wrapper is also important because Bazel's Xcode (but not CommandLineTools) wrapper crashes if you don't specify particular environment variables (replaced below). We'd need the wrapper to be invokable by clangd's --query-driver if we didn't remove the wrapper.
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
    """Reformat compile_action into a compile command clangd can understand.

    Undo Bazel-isms and figures out which files clangd should apply the command to.
    """
    compile_args = compile_action.arguments

    # Patch command by platform
    compile_args = _all_platform_patch(compile_args)
    compile_args = _apple_platform_patch(compile_args)
    # Android and Linux and grailbio LLVM toolchains: Fine as is; no special patching needed.

    source_files, header_files = _get_files(compile_args)

    return source_files, header_files, compile_args


def _convert_compile_commands(aquery_output):
    """Converts from Bazel's aquery format to de-Bazeled compile_commands.json entries.

    Input: jsonproto output from aquery, pre-filtered to (Objective-)C(++) compile actions for a given build.
    Yields: Corresponding entries for a compile_commands.json, with commas after each entry, describing all ways every file is being compiled.
        Also includes one entry per header, describing one way it is compiled (to work around https://github.com/clangd/clangd/issues/123).

    Crucially, this de-Bazels the compile commands it takes as input, leaving something clangd can understand. The result is a command that could be run from the workspace root directly, with no bazel-specific environment variables, etc.
    """

    # Process each action from Bazelisms -> file paths and their clang commands
    # Threads instead of processes because most of the execution time is farmed out to subprocesses. No need to sidestep the GIL. Might change after https://github.com/clangd/clangd/issues/123 resolved
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(32, (os.cpu_count() or 1) + 4) # Backport. Default in MIN_PY=3.8. See "using very large resources implicitly on many-core machines" in https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.ThreadPoolExecutor
    ) as threadpool:
        outputs = threadpool.map(_get_cpp_command_for_files, aquery_output.actions)

    # Yield as compile_commands.json entries
    header_files_already_written = set()
    for source_files, header_files, compile_command_args in outputs:
        # Only emit one entry per header
        # This makes the output vastly smaller, since large size has been a problem for users.
        # e.g. https://github.com/insufficiently-caffeinated/caffeine/pull/577
        # Without this, we emit an entry for each header for each time it is included, which is explosively duplicative--the same reason why C++ compilation is slow and the impetus for the new C++ modules.
        # Revert when https://github.com/clangd/clangd/issues/123 is solved, which would remove the need to emit headers, because clangd would take on that work.
        # If/when https://github.com/clangd/clangd/issues/681 gets resolved, we'd probably want to find a way to filter to one entry per platform.
        header_files_not_already_written = header_files - header_files_already_written
        header_files_already_written |= header_files_not_already_written

        for file in itertools.chain(source_files, header_files_not_already_written):
            yield {
                # Docs about compile_commands.json format: https://clang.llvm.org/docs/JSONCompilationDatabase.html#format
                'file': file,
                # Using `arguments' instead of 'command' because it's now preferred by clangd and because shlex.join doesn't work for windows cmd. For more, see https://github.com/hedronvision/bazel-compile-commands-extractor/issues/8#issuecomment-1090262263
                'arguments': compile_command_args,
                # Bazel gotcha warning: If you were tempted to use `bazel info execution_root` as the build working directory for compile_commands...search ImplementationReadme.md to learn why that breaks.
                'directory': os.environ["BUILD_WORKSPACE_DIRECTORY"],
            }


def _get_commands(target: str, flags: str):
    """Yields compile_commands.json entries for a given target and flags, gracefully tolerating errors."""
    # Log clear completion messages
    print(f"\033[0;34m>>> Analyzing commands used in {target}\033[0m", file=sys.stderr)

    # First, query Bazel's C-family compile actions for that configured target
    aquery_args = [
        'bazel',
        'aquery',
        # Aquery docs if you need em: https://docs.bazel.build/versions/master/aquery.html
        # Aquery output proto reference: https://github.com/bazelbuild/bazel/blob/master/src/main/protobuf/analysis_v2.proto
        # One bummer, not described in the docs, is that aquery filters over *all* actions for a given target, rather than just those that would be run by a build to produce a given output. This mostly isn't a problem, but can sometimes surface extra, unnecessary, misconfigured actions. Chris has emailed the authors to discuss and filed an issue so anyone reading this could track it: https://github.com/bazelbuild/bazel/issues/14156.
        f"mnemonic('(Objc|Cpp)Compile',deps({target}))",
        # We switched to jsonproto instead of proto because of https://github.com/bazelbuild/bazel/issues/13404. We could change back when fixed--reverting most of the commit that added this line and tweaking the build file to depend on the target in that issue. That said, it's kinda nice to be free of the dependency, unless (OPTIMNOTE) jsonproto becomes a performance bottleneck compated to binary protos.
        '--output=jsonproto',
        # We'll disable artifact output for efficiency, since it's large and we don't use them. Small win timewise, but dramatically less json output from aquery.
        '--include_artifacts=false',
        # Shush logging. Just for readability.
        '--ui_event_filters=-info',
        '--noshow_progress',
    ] + shlex.split(flags) + sys.argv[1:]

    aquery_process = subprocess.run(
        aquery_args,
        capture_output=True,
        encoding=locale.getpreferredencoding(),
        check=False, # We explicitly ignore errors from `bazel aquery` and carry on.
    )


    # Filter aquery error messages to just those the user should care about.
    for line in aquery_process.stderr.splitlines():
        # Shush known warnings about missing graph targets.
        # The missing graph targets are not things we want to introspect anyway.
        # Tracking issue https://github.com/bazelbuild/bazel/issues/13007.
        if line.startswith('WARNING: Targets were missing from graph:'):
            continue

        print(line, file=sys.stderr)


    # Parse proto output from aquery
    try:
        # object_hook -> SimpleNamespace allows object.member syntax, like a proto, while avoiding the protobuf dependency
        parsed_aquery_output = json.loads(aquery_process.stdout, object_hook=lambda d: types.SimpleNamespace(**d))
        # Further mimic a proto by protecting against the case where there are no actions found.
        # Otherwise, SimpleNamespace, unlike a real proto, won't create an actions attribute, leading to an AttributeError on access.
        if not hasattr(parsed_aquery_output, 'actions'):
            parsed_aquery_output.actions = []
    except json.JSONDecodeError:
        print("aquery failed. Command:", aquery_args, file=sys.stderr)
        print(f"\033[0;33m>>> Failed extracting commands for {target}\n    Continuing gracefully...\033[0m",  file=sys.stderr)
        return

    yield from _convert_compile_commands(parsed_aquery_output)


    # Log clear completion messages
    print(f"\033[0;32m>>> Finished extracting commands for {target}\033[0m", file=sys.stderr)


def _ensure_external_workspaces_link_exists():
    """Postcondition: Either //external points into Bazel's fullest set of external workspaces in output_base, or we've exited with an error that'll help the user resolve the issue."""
    is_windows = os.name == 'nt'
    source = pathlib.Path('external')

    # Traverse into output_base via bazel-out, keeping the workspace position-independent, so it can be moved without rerunning
    dest = pathlib.Path('bazel-out/../../../external')
    if is_windows:
        # On Windows, unfortunately, bazel-out is a junction, and acessing .. of a junction brings you back out the way you came. So we have to resolve bazel-out first. Not position-independent, but I think the best we can do
        dest = (pathlib.Path('bazel-out').resolve()/'../../../external').resolve()

    # Handle problem cases where //external exists
    if os.path.lexists(source):
        # Detect symlinks or Windows junctions
        # This seemed to be the cleanest way to detect both.
        # Note that os.path.islink doesn't detect junctions.
        try:
            current_dest = os.readlink(source) # MIN_PY=3.9 source.readlink()
        except OSError:
            print("\033[0;31m>>> //external already exists, but it isn't a symlink or Windows junction. //external is reserved by Bazel and needed for this tool. Please rename or delete your existing //external and rerun. More details in the README if you want them.\033[0m", file=sys.stderr) # Don't auto delete in case the user has something important there.
            exit(1)
        
        # Normalize the path for matching
        # First, workaround a gross case where Windows readlink returns extended path, starting with \\?\, causing the match to fail
        if is_windows and current_dest.startswith('\\\\?\\'):
            current_dest = current_dest[4:] # MIN_PY=3.9 stripprefix
        current_dest = pathlib.Path(current_dest)

        if dest != current_dest:
            print("\033[0;33m>>> //external links to the wrong place. Automatically deleting and relinking...\033[0m", file=sys.stderr)
            source.unlink()

    # Create link if it doesn't already exist
    if not os.path.lexists(source):
        if is_windows:
            # We create a junction on Windows because symlinks need more than default permissions (ugh). Without an elevated prompt or a system in developer mode, symlinking would fail with get "OSError: [WinError 1314] A required privilege is not held by the client:"
            subprocess.run(f'mklink /J "{source}" "{dest}"', check=True, shell=True) # shell required for mklink builtin
        else:
            source.symlink_to(dest, target_is_directory=True)
        print(f"""\033[0;32m>>> Automatically added //external workspace link:
    This link makes it easy for you—and for build tooling—to see the external dependencies you bring in. It also makes your source tree have the same directory structure as the build sandbox.
    It's a win/win: It's easier for you to browse the code you use, and it eliminates whole categories of edge cases for build tooling.\033[0m""", file=sys.stderr)


def _ensure_gitignore_entries():
    """Postcondition: compile_commands.json and the external symlink are .gitignore'd, if it looks like they're using git."""
    needed_entries = [
        '/external', # Differs on Windows vs macOS/Linux, so we can't check it in. Needs to not have trailing / because it's a symlink on macOS/Linux
        '/bazel-*', # Bazel output symlinks. Same reasons as external.
        '/compile_commands.json', # Compiled output -> don't check in
        '/.cache/', # Where clangd puts its indexing work
    ]

    # Separate operations because Python doesn't have a built in mode for read/write, don't truncate, create, allow seek to beginning of file. 
    open('.gitignore', 'a').close() # Ensure .gitignore exists
    with open('.gitignore') as gitignore:
        lines = [l.rstrip() for l in gitignore]
    to_add = [e for e in needed_entries if e not in lines]
    with open('.gitignore', 'w') as gitignore:
        # Rewriting all the lines solves the case of a missing trailing \n
        for line in itertools.chain(lines, to_add):
            gitignore.write(line)
            gitignore.write('\n')
    if to_add:
        print(f"\033[0;32m>>> Automatically added {to_add} to .gitignore to avoid problems.\033[0m", file=sys.stderr)


if __name__ == '__main__':
    workspace_root = pathlib.Path(os.environ['BUILD_WORKSPACE_DIRECTORY']) # Set by `bazel run`
    os.chdir(workspace_root) # Ensure the working directory is the workspace root. Assumed by future commands.

    _ensure_gitignore_entries()
    _ensure_external_workspaces_link_exists()

    target_flag_pairs = [
        # Begin: template filled by Bazel
        {target_flag_pairs}
        # End:   template filled by Bazel
    ]
    compile_command_entries = []
    for (target, flags) in target_flag_pairs:
        compile_command_entries.extend(_get_commands(target, flags))

    # Chain output into compile_commands.json
    with open('compile_commands.json', 'w') as output_file:
        json.dump(
            compile_command_entries,
            output_file,
            indent=2, # Yay, human readability!
            check_circular=False # For speed.
        )
