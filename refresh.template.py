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
import tempfile
import time
import types
import typing # MIN_PY=3.9: Switch e.g. typing.List[str] -> list[str]


def _print_header_finding_warning_once():
    """Gives users context about "compiler errors" while header finding. Namely that we're recovering."""
    # Shared between platforms

    # Just log once; subsequent messages wouldn't add anything.
    if _print_header_finding_warning_once.has_logged: return
    _print_header_finding_warning_once.has_logged = True

    print("""\033[0;33m>>> While locating the headers you use, we encountered a compiler warning or error.
    No need to worry; your code doesn't have to compile for this tool to work.
    However, we'll still print the errors and warnings in case they're helpful for you in fixing them.
    If the errors are about missing files that Bazel should generate:
        You might want to run a build of your code with --keep_going.
        That way, everything possible is generated, browsable and indexed for autocomplete.
    But, if you have *already* built your code successfully:
        Please make sure you're supplying this tool with the same flags you use to build.
        You can either use a refresh_compile_commands rule or the special -- syntax. Please see the README.
        [Supplying flags normally won't work. That just causes this tool to be built with those flags.]
    Continuing gracefully...\033[0m""",  file=sys.stderr)
_print_header_finding_warning_once.has_logged = False


@functools.lru_cache(maxsize=None)
def _get_bazel_cached_action_keys():
    """Gets the set of actionKeys cached in bazel-out."""
    action_cache_process = subprocess.run(
        ['bazel', 'dump', '--action_cache'],
        capture_output=True,
        encoding=locale.getpreferredencoding(),
        check=True, # Should always succeed.
    )

    action_keys = set()
    marked_as_empty = False # Sometimes the action cache is empty...despite having built this file, so we have to handle that case. See https://github.com/hedronvision/bazel-compile-commands-extractor/issues/64
    for line in action_cache_process.stdout.splitlines():
        line = line.strip()
        if line.startswith('actionKey = '):
            action_keys.add(line[12:]) # Remainder after actionKey =
        elif line.startswith('Action cache (0 records):'):
            marked_as_empty = True

    # Make sure we get notified of changes to the format, since bazel dump --action_cache isn't public API.
    # We continue gracefully, rather than asserting, because we can (conservatively) continue without hitting cache.
    if not marked_as_empty and not action_keys:
        print("\033[0;33m>>> Failed to get action keys from Bazel.\nPlease file an issue with the following log:\n\033[0m"+action_cache_process.stdout,  file=sys.stderr)

    return action_keys


def _parse_headers_from_makefile_deps(d_file_content: str, source_path_for_sanity_check: typing.Optional[str] = None):
    """Parses a set of headers from the contents of a .d makefile dependency file created with the -M* or -dependencies option to gcc/clang.

    See https://clang.llvm.org/docs/ClangCommandLineReference.html#dependency-file-generation for more.
    """
    # Makefile formal looks like:
    # foo.o[optional space]: foo.cc bar.h \
    # baz.hpp (etc.)
    colon_idx = d_file_content.index(':')
    assert d_file_content[:colon_idx].strip().endswith('.o'), "Something went wrong in makefile parsing to get headers. Zeroth entry should be the object file. Output:\n" + d_file_content
    split = d_file_content[colon_idx+1:].replace('\\\n', '').split() # Undo shell line wrapping bc it's not consistent (depends on file name length). Also, makefiles don't seem to really support escaping spaces, so we'll punt that case https://stackoverflow.com/questions/30687828/how-to-escape-spaces-inside-a-makefile
    assert source_path_for_sanity_check is None or split[0].endswith(source_path_for_sanity_check), "Something went wrong in makefile parsing to get headers. First entry should be the source file. Output:\n" + d_file_content
    headers = split[1:] # Remove .o and source entries (since they're not headers). Verified above
    headers = set(headers) # Make unique. GCC sometimes emits duplicate entries https://github.com/hedronvision/bazel-compile-commands-extractor/issues/7#issuecomment-975109458
    return headers


@functools.lru_cache(maxsize=None)
def _get_cached_adjusted_modified_time(path: str):
    """A fast (cached) way to get the modified time of files.

    Otherwise, most of our runtime in the cached case ends up being mtime stat'ing the same headers over and over again.

    Intended for checking whether header include caches are fresh.
    Contains some adjustments to make checking as simple as comparing modified times.
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:  # File doesn't exist.
        return 0  # For our purposes, doesn't exist means we don't have a newer version, so we'll return a very old time that'll always qualify the cache as fresh in a comparison.
        # Two cases here:
        # (1) Somehow it weren't generated in the build that created the depfile. We therefore won't get any fresher by building, so we'll treat that as good enough.
        # (2) Or it's been deleted since we last cached, in which case we'd rather use the cached version if its otherwise fresh.

    # Bazel internal sources have timestamps 10y in the future as part of an mechanism to detect and prevent modification, so we'll similarly ignore those, since they shouldn't be changing.
    if mtime > BAZEL_INTERNAL_SOURCE_CUTOFF:
        return 0

    return mtime
BAZEL_INTERNAL_SOURCE_CUTOFF = time.time() + 60*60*24*365  # 1year in to the future. Safely below bazel's 10y margin, but high enough that no sane normal file should be past this.


def _get_headers_gcc(compile_args: typing.List[str], source_path: str, action_key: str):
    """Gets the headers used by a particular compile command that uses gcc arguments formatting (including clang.)

    Relatively slow. Requires running the C preprocessor if we can't hit Bazel's cache.
    """
    # Flags reference here: https://clang.llvm.org/docs/ClangCommandLineReference.html

    # Check to see if Bazel has an (approximately) fresh cache of the included headers, and if so, use them to avoid a slow preprocessing step.
    if action_key in _get_bazel_cached_action_keys():  # Safe because Bazel only holds one cached action key per path, and the key contains the path.
        for i, arg in enumerate(compile_args):
            if arg.startswith('-MF'):
                if len(arg) > 3: # Either appended, like -MF<file>
                    dep_file_path = arg[:3]
                else: # Or after as a separate arg, like -MF <file>
                    dep_file_path = compile_args[i+1]
                if os.path.isfile(dep_file_path):
                    dep_file_last_modified = os.path.getmtime(dep_file_path) # Do before opening just as a basic hedge against concurrent write, even though we won't handle the concurrent delete case perfectly.
                    with open(dep_file_path) as dep_file:
                        dep_file_contents = dep_file.read()
                    headers = _parse_headers_from_makefile_deps(dep_file_contents)
                    # Check freshness of dep file by making sure none of the files in it have been modified since its creation.
                    if (_get_cached_adjusted_modified_time(source_path) <= dep_file_last_modified
                        and all(_get_cached_adjusted_modified_time(header_path) <= dep_file_last_modified for header_path in headers)):
                        return headers # Fresh cache! exit early.
                break

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

    # Tolerate failure gracefully--during editing the code may not compile!
    if header_search_process.stderr:
        _print_header_finding_warning_once()
        print(header_search_process.stderr, file=sys.stderr, end='') # Captured with capture_output and dumped explicitly to avoid interlaced output.

    if not header_search_process.stdout: # Worst case, we couldn't get the headers,
        return set()
    # But often, we can get the headers, despite the error.

    return _parse_headers_from_makefile_deps(header_search_process.stdout)


def windows_list2cmdline(seq):
    """
    Copied from list2cmdline in https://github.com/python/cpython/blob/main/Lib/subprocess.py because we need it but it's not exported as part of the public API.

    Translate a sequence of arguments into a command line
    string, using the same rules as the MS C runtime:
    1) Arguments are delimited by white space, which is either a
       space or a tab.
    2) A string surrounded by double quotation marks is
       interpreted as a single argument, regardless of white space
       contained within.  A quoted string can be embedded in an
       argument.
    3) A double quotation mark preceded by a backslash is
       interpreted as a literal double quotation mark.
    4) Backslashes are interpreted literally, unless they
       immediately precede a double quotation mark.
    5) If backslashes immediately precede a double quotation mark,
       every pair of backslashes is interpreted as a literal
       backslash.  If the number of backslashes is odd, the last
       backslash escapes the next double quotation mark as
       described in rule 3.
    """

    # See
    # http://msdn.microsoft.com/en-us/library/17w5ykft.aspx
    # or search http://msdn.microsoft.com for
    # "Parsing C++ Command-Line Arguments"
    result = []
    needquote = False
    for arg in map(os.fsdecode, seq):
        bs_buf = []

        # Add a space to separate this argument from the others
        if result:
            result.append(' ')

        needquote = (" " in arg) or ("\t" in arg) or not arg
        if needquote:
            result.append('"')

        for c in arg:
            if c == '\\':
                # Don't know if we need to double yet.
                bs_buf.append(c)
            elif c == '"':
                # Double backslashes.
                result.append('\\' * len(bs_buf)*2)
                bs_buf = []
                result.append('\\"')
            else:
                # Normal char
                if bs_buf:
                    result.extend(bs_buf)
                    bs_buf = []
                result.append(c)

        # Add remaining backslashes, if any.
        if bs_buf:
            result.extend(bs_buf)

        if needquote:
            result.extend(bs_buf)
            result.append('"')

    return ''.join(result)


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

    def _search_headers(command):
        return subprocess.run(
            command,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            env=environment,
            encoding=locale.getpreferredencoding(),
            check=False, # We explicitly ignore errors and carry on.
        )

    try:
        header_search_process = _search_headers(header_cmd)
    except WindowsError as e:
        # Handle case where command line length is exceeded and we need a param file.
            # See https://docs.microsoft.com/en-us/troubleshoot/windows-client/shell-experience/command-line-string-limitation
        # We handle the error instead of calculating the command length because the length includes escaping internal to the subprocess.run call
        if e.winerror == 206:  # Thrown when command is too long, despite the error message being "The filename or extension is too long". For a few more details see also https://stackoverflow.com/questions/2381241/what-is-the-subprocess-popen-max-length-of-the-args-parameter
            # Write header_cmd to a temporary file, so we can use it as a parameter file to cl.exe.
            # E.g. cl.exe @params_file.txt
            # tempfile.NamedTemporaryFile doesn't work because cl.exe can't open it--as the Python docs would indicate--so we have to do cleanup ourselves.
            fd, path = tempfile.mkstemp(text=True)
            try:
                os.write(fd, windows_list2cmdline(header_cmd[1:]).encode()) # should skip cl.exe the 1st line.
                os.close(fd)
                header_search_process = _search_headers([header_cmd[0], f'@{path}'])
            finally: # Safe cleanup even in the event of an error
                os.remove(path)
        else: # Some other WindowsError we didn't mean to catch.
            raise

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
        if source_path.endswith('/' + line) or source_path == line: # Munching the source filename echoed the first part of the include output
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


def _is_relative_to(sub: pathlib.PurePath, parent: pathlib.PurePath):
    """Helper to determine if one path is relative to another"""
    try:
        # MIN_PY=3.9: Eliminate helper in favor of PurePath.is_relative_to()
        sub.relative_to(parent)
        return True
    except ValueError:
        return False


def _file_is_in_main_workspace_and_not_external(file_str: str):
    file_path = pathlib.PurePath(file_str)
    if file_path.is_absolute():
        workspace_absolute = pathlib.PurePath(os.environ["BUILD_WORKSPACE_DIRECTORY"])
        if not _is_relative_to(file_path, workspace_absolute):
            return False
        file_path = file_path.relative_to(workspace_absolute)
    # You can now assume that the path is relative to the workspace.
    # [Already assuming that relative paths are relative to the main workspace.]

    # some/file.h, but not external/some/file.h
    # also allows for things like bazel-out/generated/file.h
    if _is_relative_to(file_path, pathlib.PurePath("external")):
        return False

    # ... but, ignore files in e.g. bazel-out/<configuration>/bin/external/
    if file_path.parts[0] == 'bazel-out' and file_path.parts[3] == 'external':
        return False

    return True


def _get_headers(compile_action, source_path: str):
    """Gets the headers used by a particular compile command.

    Relatively slow. Requires running the C preprocessor.
    """
    # Hacky, but hopefully this is a temporary workaround for the clangd issue mentioned in the caller (https://github.com/clangd/clangd/issues/123)
    # Runs a modified version of the compile command to piggyback on the compiler's preprocessing and header searching.

    # As an alternative approach, you might consider trying to get the headers by inspecting the Middlemen actions in the aquery output, but I don't see a way to get just the ones actually #included--or an easy way to get the system headers--without invoking the preprocessor's header search logic.
        # For more on this, see https://github.com/hedronvision/bazel-compile-commands-extractor/issues/5#issuecomment-1031148373

    if {exclude_headers} == "all":
        return set()
    elif {exclude_headers} == "external" and compile_action.is_external:
        # Shortcut - an external action can't include headers in the workspace (or, non-external headers)
        return set()

    output_file = None
    for i, arg in enumerate(compile_action.arguments):
        if arg == '-o': # clang/gcc. Docs https://clang.llvm.org/docs/ClangCommandLineReference.html
            output_file = compile_action.arguments[i+1]
        elif arg.startswith('/Fo'): # MSVC *and clang*. MSVC docs https://docs.microsoft.com/en-us/cpp/build/reference/compiler-options-listed-alphabetically?view=msvc-170
            output_file = arg[3:]
    # Since our output file parsing isn't complete, fall back on a warning message to solicit help.
    # A more full (if more involved) solution would be to get the primaryOutput for the action from the aquery output, but this should handle the cases Bazel emits.
    if not output_file and not _get_headers.has_logged:
        _get_headers.has_logged = True
        print(f"""\033[0;33m>>> Please file an issue containing the following: Output file not detected in arguments {compile_action.arguments}.
    Not a big deal; things will work but will be a little slower.
    Thanks for your help!
    Continuing gracefully...\033[0m""",  file=sys.stderr)

    # Check for a fresh cache of headers
    if output_file:
        cache_file_path = output_file + ".hedron.compile-commands.headers" # Embed our cache in bazel's
        if os.path.isfile(cache_file_path):
            cache_last_modified = os.path.getmtime(cache_file_path) # Do before opening just as a basic hedge against concurrent write, even though we won't handle the concurrent delete case perfectly.
            try:
                with open(cache_file_path) as cache_file:
                    action_key, headers = json.load(cache_file)
            except json.JSONDecodeError:
                # Corrupted cache, which can happen if, for example, the user kills the program, since writes aren't atomic.
                # But if it is the result of a bug, we want to print it before it's overwritten, so it can be reported
                # For a real instance, see https://github.com/hedronvision/bazel-compile-commands-extractor/issues/60
                with open(cache_file_path) as cache_file:
                    print(f"""\033[0;33m>>> Ignoring corrupted header cache {cache_file_path}
    This is okay if you manually killed this tool earlier.
    But if this message is appearing spontaneously or frequently, please file an issue containing the contents of the corrupted cache, below.
    {cache_file.read()}
    Thanks for your help!
    Continuing gracefully...\033[0m""",  file=sys.stderr)
            else:
                # Check cache freshness.
                    # Action key validates that it corresponds to the same action arguments
                    # And we also need to check that there aren't newer versions of the files
                if (action_key == compile_action.actionKey
                    and _get_cached_adjusted_modified_time(source_path) <= cache_last_modified
                    and all(_get_cached_adjusted_modified_time(header_path) <= cache_last_modified for header_path in headers)):
                    return set(headers)

    if compile_action.arguments[0].endswith('cl.exe'): # cl.exe and also clang-cl.exe
        headers = _get_headers_msvc(compile_action.arguments, source_path)
    else:
        headers = _get_headers_gcc(compile_action.arguments, source_path, compile_action.actionKey)

    # Cache for future use
    if output_file:
        os.makedirs(os.path.dirname(cache_file_path), exist_ok=True)
        with open(cache_file_path, 'w') as cache_file:
            json.dump((compile_action.actionKey, list(headers)), cache_file)

    if {exclude_headers} == "external":
        headers = {header for header in headers if _file_is_in_main_workspace_and_not_external(header)}

    return headers
_get_headers.has_logged = False


def _get_files(compile_action):
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
    compile_only_flag = '/c' if '/c' in compile_action.arguments else '-c' # For Windows/msvc support
    assert compile_only_flag in compile_action.arguments, f"/c or -c, required for parsing sources, is not found in compile args: {compile_action.arguments}"
    source_index = compile_action.arguments.index(compile_only_flag) + 1
    source_file = compile_action.arguments[source_index]
    SOURCE_EXTENSIONS = ('.c', '.cc', '.cpp', '.cxx', '.c++', '.C', '.m', '.mm', '.cu', '.cl', '.s', '.asm', '.S')
    assert source_file.endswith(SOURCE_EXTENSIONS), f"Source file not found after {compile_only_flag} in {compile_action.arguments}"
    assert source_index + 1 == len(compile_action.arguments) or compile_action.arguments[source_index + 1].startswith('-') or not compile_action.arguments[source_index + 1].endswith(SOURCE_EXTENSIONS), f"Multiple sources detected after {compile_only_flag}. Might work, but needs testing, and unlikely to be right given Bazel's incremental compilation. CMD: {compile_action.arguments}"

    # Warn gently about missing files
    file_exists = os.path.isfile(source_file)
    if not file_exists:
        if not _get_files.has_logged_missing_file_error: # Just log once; subsequent messages wouldn't add anything.
            _get_files.has_logged_missing_file_error = True
            print(f"""\033[0;33m>>> A source file you compile doesn't (yet) exist: {source_file}
    It's probably a generated file, and you haven't yet run a build to generate it.
    That's OK; your code doesn't even have to compile for this tool to work.
    If you can, though, you might want to run a build of your code.
        That way everything is generated, browsable and indexed for autocomplete.
    However, if you have *already* built your code, and generated the missing file...
        Please make sure you're supplying this tool with the same flags you use to build.
        You can either use a refresh_compile_commands rule or the special -- syntax. Please see the README.
        [Supplying flags normally won't work. That just causes this tool to be built with those flags.]
    Continuing gracefully...\033[0m""",  file=sys.stderr)

    # Note: We need to apply commands to headers and sources.
    # Why? clangd currently tries to infer commands for headers using files with similar paths. This often works really poorly for header-only libraries. The commands should instead have been inferred from the source files using those libraries... See https://github.com/clangd/clangd/issues/123 for more.
    # When that issue is resolved, we can stop looking for headers and just return the single source file.
    return {source_file}, _get_headers(compile_action, source_file) if file_exists else set()
_get_files.has_logged_missing_file_error = False


@functools.lru_cache(maxsize=None)
def _get_apple_SDKROOT(SDK_name: str):
    """Get path to xcode-select'd root for the given OS."""
    SDKROOT_maybe_versioned =  subprocess.check_output(
        ('xcrun', '--show-sdk-path', '-sdk', SDK_name.lower()),
        stderr=subprocess.DEVNULL,
        encoding=locale.getpreferredencoding()
    ).rstrip()
    # Unless xcode-select has been invoked (like for a beta) we'd expect, e.g.,  '/Applications/Xcode.app/Contents/Developer/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS<version>.sdk' or '/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk'.
    version = subprocess.check_output(
        ('xcrun', '--show-sdk-version', '-sdk', SDK_name.lower()),
        stderr=subprocess.DEVNULL,
        encoding=locale.getpreferredencoding()
    ).rstrip()
    return SDKROOT_maybe_versioned.replace(version, '') # Strip version and use unversioned SDK symlink so the compile commands are still valid after an SDK update.
    # Traditionally stored in SDKROOT environment variable, but not provided by Bazel. See https://github.com/bazelbuild/bazel/issues/12852


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
    # Unless xcode-select has been invoked (like for a beta) we'd expect, e.g., '/Applications/Xcode.app/Contents/Developer' or '/Library/Developer/CommandLineTools'.
    # Traditionally stored in DEVELOPER_DIR environment variable, but not provided by Bazel. See https://github.com/bazelbuild/bazel/issues/12852


@functools.lru_cache(maxsize=None)
def _get_apple_active_clang():
    """Get path to xcode-select'd clang version."""
    return subprocess.check_output(
        ('xcrun', '--find', 'clang'),
        stderr=subprocess.DEVNULL, # Suppress superfluous error messages like "Requested but did not find extension point with identifier..."
        encoding=locale.getpreferredencoding()
    ).rstrip()
    # Unless xcode-select has been invoked (like for a beta) we'd expect, e.g., '/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang' or '/Library/Developer/CommandLineTools/usr/bin/clang'.


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
        compile_args = [arg for arg in compile_args if not arg.startswith('DEBUG_PREFIX_MAP_PWD') or arg == 'OSO_PREFIX_MAP_PWD'] # No need for debug prefix maps if compiling in place, not that we're compiling anyway.
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
    # Patch command by platform
    compile_action.arguments = _all_platform_patch(compile_action.arguments)
    compile_action.arguments = _apple_platform_patch(compile_action.arguments)
    # Android and Linux and grailbio LLVM toolchains: Fine as is; no special patching needed.

    source_files, header_files = _get_files(compile_action)

    return source_files, header_files, compile_action.arguments


def _convert_compile_commands(aquery_output):
    """Converts from Bazel's aquery format to de-Bazeled compile_commands.json entries.

    Input: jsonproto output from aquery, pre-filtered to (Objective-)C(++) compile actions for a given build.
    Yields: Corresponding entries for a compile_commands.json, with commas after each entry, describing all ways every file is being compiled.
        Also includes one entry per header, describing one way it is compiled (to work around https://github.com/clangd/clangd/issues/123).

    Crucially, this de-Bazels the compile commands it takes as input, leaving something clangd can understand. The result is a command that could be run from the workspace root directly, with no bazel-specific environment variables, etc.
    """

    # Tag actions as external if we're going to need to know that later.
    if {exclude_external_sources} or {exclude_headers} == "external":
        targets_by_id = {target.id : target.label for target in aquery_output.targets}

        def _amend_action_as_external(action):
            """Tag action as external if it's created by an external target"""
            target = targets_by_id[action.targetId] # Should always be present. KeyError as implicit assert.
            assert not target.startswith("@//"), f"Expecting local targets to start with // in aquery output. Found @// for action {action}, target {target}"
            assert not target.startswith("//external"), f"Expecting external targets will start with @. Found //external for action {action}, target {target}"

            action.is_external = target.startswith("@")
            return action

        aquery_output.actions = (_amend_action_as_external(action) for action in aquery_output.actions)

        if {exclude_external_sources}:
            aquery_output.actions = filter(lambda action: not action.is_external, aquery_output.actions)

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

    additional_flags = shlex.split(flags) + sys.argv[1:]

    # Detect any positional args--build targets--in the flags, and issue a warning.
    if any(not f.startswith('-') for f in additional_flags) or '--' in additional_flags[:-1]:
        print("""\033[0;33m>>> The flags you passed seem to contain targets.
    Try adding them as targets in your refresh_compile_commands rather than flags.
    [Specifying targets at runtime isn't supported yet, and in a moment, Bazel will likely fail to parse without our help. If you need to be able to specify targets at runtime, and can't easily just add them to your refresh_compile_commands, please open an issue or file a PR. You may also want to refer to https://github.com/hedronvision/bazel-compile-commands-extractor/issues/62.]\033[0m""",  file=sys.stderr)

    # Quick (imperfect) effort at detecting flags in the targets.
    # Can't detect flags starting with -, because they could be subtraction patterns.
    if any(target.startswith('--') for target in shlex.split(target)):
        print("""\033[0;33m>>> The target you specified seems to contain flags.
    Try adding them as flags in your refresh_compile_commands rather than targets.
    In a moment, Bazel will likely fail to parse.\033[0m""",  file=sys.stderr)

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
        # Disable param files, which would obscure compile actions
        # Mostly, people enable param files on Windows to avoid the relatively short command length limit.
            # For more, see compiler_param_file in https://bazel.build/docs/windows
            # They are, however, technically supported on other platforms/compilers.
        # That's all well and good, but param files would prevent us from seeing compile actions before the param files had been generated by compilation.
        # Since clangd has no such length limit, we'll disable param files for our aquery run.
        '--features=-compiler_param_file',
    ] + additional_flags

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
        print("Bazel aquery failed. Command:", aquery_args, file=sys.stderr)
        print(f"\033[0;33m>>> Failed extracting commands for {target}\n    Continuing gracefully...\033[0m",  file=sys.stderr)
        return

    yield from _convert_compile_commands(parsed_aquery_output)


    # Log clear completion messages
    print(f"\033[0;32m>>> Finished extracting commands for {target}\033[0m", file=sys.stderr)


def _ensure_external_workspaces_link_exists():
    """Postcondition: Either //external points into Bazel's fullest set of external workspaces in output_base, or we've exited with an error that'll help the user resolve the issue."""
    is_windows = os.name == 'nt'
    source = pathlib.Path('external')

    if not os.path.lexists('bazel-out'):
        print("\033[0;31m>>> //bazel-out is missing. Please remove --symlink-prefix, so the workspace mirrors the compilation environment.\033[0m", file=sys.stderr)
        # Crossref: https://github.com/hedronvision/bazel-compile-commands-extractor/issues/14 https://github.com/hedronvision/bazel-compile-commands-extractor/pull/65
        # Note: No longer experimental_no_product_name_out_symlink. See https://github.com/bazelbuild/bazel/commit/06bd3e8c0cd390f077303be682e9dec7baf17af2
        exit(1)

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
            print(f"\033[0;31m>>> //external already exists, but it isn't a {'junction' if is_windows else 'symlink'}. //external is reserved by Bazel and needed for this tool. Please rename or delete your existing //external and rerun. More details in the README if you want them.\033[0m", file=sys.stderr) # Don't auto delete in case the user has something important there.
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
        print("""\033[0;32m>>> Automatically added //external workspace link:
    This link makes it easy for you--and for build tooling--to see the external dependencies you bring in. It also makes your source tree have the same directory structure as the build sandbox.
    It's a win/win: It's easier for you to browse the code you use, and it eliminates whole categories of edge cases for build tooling.\033[0m""", file=sys.stderr)


def _ensure_gitignore_entries():
    """Postcondition: compile_commands.json and the external symlink are .gitignore'd, if it looks like they're using git."""
    # Silently check that we're in a git repo--and no-op if not.
    if (not os.path.isfile('.gitignore') # Still add to the .gitignore if it exists, even if git isn't installed.
        and subprocess.run('git rev-parse --git-dir', # see https://stackoverflow.com/questions/2180270/check-if-current-directory-is-a-git-repository
        shell=True, # Unifies error case where git isn't even installed by making it also a non-zero exit code w/ no exception
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode): # non-zero indicates not in git repository
        # Note that we're also handling the case where the bazel workspace is nested inside the git repository; it's not enough to just check for the presence of a .git directory
        return

    needed_entries = [ # Pattern followed by an explainer comment that we'll add to the gitignore
        ('/external', "# The external link: Differs on Windows vs macOS/Linux, so we can't check it in. The pattern needs to not have a trailing / because it's a symlink on macOS/Linux."),
        ('/bazel-*', "# Bazel output symlinks: Same reasoning as /external. You need the * because people can change the name of the directory your repository is cloned into, changing the bazel-<workspace_name> symlink."),
        ('/compile_commands.json', "# Compiled output -> don't check in"),
        ('/.cache/', "# Directory where clangd puts its indexing work"),
    ]

    # Separate operations because Python doesn't have a built in mode for read/write, don't truncate, create, allow seek to beginning of file.
    open('.gitignore', 'a').close() # Ensure .gitignore exists
    with open('.gitignore') as gitignore:
        lines = [l.rstrip() for l in gitignore]
    to_add = [entry for entry in needed_entries if entry[0] not in lines]
    if to_add: # Add a nice header
        # Ensure spacer before header
        if lines and lines[-1]:
            lines.append("")
        lines.append("### Added by Hedron's Bazel Compile Commands Extractor: https://github.com/hedronvision/bazel-compile-commands-extractor")
        for entry in to_add:
            lines.extend(entry[::-1]) # Explanatory comment, then pattern
    with open('.gitignore', 'w') as gitignore:
        # Rewriting all the lines solves the case of a missing trailing \n
        for line in lines:
            gitignore.write(line)
            gitignore.write('\n')
    if to_add:
        print("\033[0;32m>>> Automatically added entries to .gitignore to avoid problems.\033[0m", file=sys.stderr)


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
