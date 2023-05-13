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


# This file requires python 3.6, which is enforced by check_python_version.template.py
# 3.6 backwards compatibility required by @zhanyong-wan in https://github.com/hedronvision/bazel-compile-commands-extractor/issues/111.
# 3.7 backwards compatibility required by @lummax in https://github.com/hedronvision/bazel-compile-commands-extractor/pull/27.
# ^ Try to contact before upgrading.
# When adding things could be cleaner if we had a higher minimum version, please add a comment with MIN_PY=3.<v>.
# Similarly, when upgrading, please search for that MIN_PY= tag.


import concurrent.futures
import enum
import functools  # MIN_PY=3.9: Replace `functools.lru_cache(maxsize=None)` with `functools.cache`.
import itertools
import json
import locale
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import types
import typing # MIN_PY=3.9: Switch e.g. typing.List[str] -> list[str]


@enum.unique
class SGR(enum.Enum):
    """Enumerate (some of the) available SGR (Select Graphic Rendition) control sequences."""
    # For details on SGR control sequences (and ANSI escape codes in general), see: https://en.wikipedia.org/wiki/ANSI_escape_code#SGR_(Select_Graphic_Rendition)_parameters
    RESET = '\033[0m'
    FG_RED = '\033[0;31m'
    FG_GREEN = '\033[0;32m'
    FG_YELLOW = '\033[0;33m'
    FG_BLUE = '\033[0;34m'


def _log_with_sgr(sgr, colored_message, uncolored_message=''):
    """Log a message to stderr wrapped in an SGR context."""
    print(sgr.value, colored_message, SGR.RESET.value, uncolored_message, sep='', file=sys.stderr, flush=True)


def log_error(colored_message, uncolored_message=''):
    """Log an error message (in red) to stderr."""
    _log_with_sgr(SGR.FG_RED, colored_message, uncolored_message)


def log_warning(colored_message, uncolored_message=''):
    """Log a warning message (in yellow) to stderr."""
    _log_with_sgr(SGR.FG_YELLOW, colored_message, uncolored_message)


def log_info(colored_message, uncolored_message=''):
    """Log an informative message (in blue) to stderr."""
    _log_with_sgr(SGR.FG_BLUE, colored_message, uncolored_message)


def log_success(colored_message, uncolored_message=''):
    """Log a success message (in green) to stderr."""
    _log_with_sgr(SGR.FG_GREEN, colored_message, uncolored_message)


def _print_header_finding_warning_once():
    """Gives users context about "compiler errors" while header finding. Namely that we're recovering."""
    # Shared between platforms

    # Just log once; subsequent messages wouldn't add anything.
    if _print_header_finding_warning_once.has_logged: return
    _print_header_finding_warning_once.has_logged = True

    log_warning(""">>> While locating the headers you use, we encountered a compiler warning or error.
    No need to worry; your code doesn't have to compile for this tool to work.
    However, we'll still print the errors and warnings in case they're helpful for you in fixing them.
    If the errors are about missing files that Bazel should generate:
        You might want to run a build of your code with --keep_going.
        That way, everything possible is generated, browsable and indexed for autocomplete.
    But, if you have *already* built your code successfully:
        Please make sure you're supplying this tool with the same flags you use to build.
        You can either use a refresh_compile_commands rule or the special -- syntax. Please see the README.
        [Supplying flags normally won't work. That just causes this tool to be built with those flags.]
    Continuing gracefully...""")
_print_header_finding_warning_once.has_logged = False


@functools.lru_cache(maxsize=None)
def _get_bazel_cached_action_keys():
    """Gets the set of actionKeys cached in bazel-out."""
    action_cache_process = subprocess.run(
        ['bazel', 'dump', '--action_cache'],
        # MIN_PY=3.7: Replace PIPEs with capture_output.
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
        log_warning(">>> Failed to get action keys from Bazel.\nPlease file an issue with the following log:\n", action_cache_process.stdout)

    return action_keys


def _parse_headers_from_makefile_deps(d_file_content: str, source_path_for_sanity_check: typing.Optional[str] = None):
    """Parse a set of headers from the contents of a `*.d` dependency file generated by clang (or gcc).

    A dependency file can be generated with the `-M`/`--dependencies` flag or its friends.
    See https://clang.llvm.org/docs/ClangCommandLineReference.html#dependency-file-generation for more details.
    """
    # When reading file content as text with universal newlines mode enabled (the default), Python converts OS-specific line endings to '\n' (see https://docs.python.org/3/library/functions.html#open-newline-parameter for the thrilling details).
    # This function takes an arbitrary string, so we also ensure no `\r` characters have snuck through, because that's almost certainly an upstream error.
    assert '\r' not in d_file_content, "Something went wrong in makefile parsing to get headers. Dependency file content should not contain literal '\r' characters. Output:\n" + repr(d_file_content)

    # We assume that this Makefile-like dependency file (`*.d`) contains exactly one `target: dependencies` rule.
    # There can be an optional space after the target, and long lists of dependencies (often) carry over with a backslash and newline.
    # For example, `d_file_content` might be: `"foo.o : foo.cc bar.h \\\n     baz.hpp"`.
    target, dependencies = d_file_content.split(':', 1)
    target = target.strip()  # Remove the optional trailing space.
    assert target.endswith('.o'), "Something went wrong in makefile parsing to get headers. The target should be an object file. Output:\n" + d_file_content
    # Undo shell-like line wrapping because the newlines aren't eaten by shlex.join. Note also that it's the line wrapping is inconsistently generated across compilers and depends on the lengths of the filenames, so you can't just split on the escaped newlines.
    dependencies = dependencies.replace('\\\n', '')
    # On Windows, swap out (single) backslash path directory separators for forward slash. Shlex otherwise eats the separators...and Windows gcc intermixes backslash separators with backslash escaped spaces. For a real example of gcc run from Windows, see https://github.com/hedronvision/bazel-compile-commands-extractor/issues/81
    if os.name == 'nt':
        dependencies = re.sub(r'\\(?=[^ \\])', '/', dependencies)
    # We'll use shlex.split as a good proxy for escaping, but note that Makefiles themselves [don't seem to really support escaping spaces](https://stackoverflow.com/questions/30687828/how-to-escape-spaces-inside-a-makefile).
    dependencies = shlex.split(dependencies)
    source, *headers = dependencies  # The first dependency is a source entry, only used to (optionally) sanity-check the dependencies if a source path is provided.
    assert source_path_for_sanity_check is None or source.endswith(source_path_for_sanity_check), "Something went wrong in makefile parsing to get headers. The first dependency should be the source file. Output:\n" + d_file_content
    # Make the headers unique, because GCC [sometimes emits duplicate entries](https://github.com/hedronvision/bazel-compile-commands-extractor/issues/7#issuecomment-975109458).
    return set(headers)


@functools.lru_cache(maxsize=None)
def _get_cached_adjusted_modified_time(path: str):
    """Get (and cache!) the modified time of a file, slightly adjusted for easy comparison.

    This is primarily intended to check whether header include caches are fresh.

    If the file doesn't exist or is inaccessible (either because it was deleted or wasn't generated), return 0.
    For bazel's internal sources, which have timestamps 10 years in the future, also return 0.

    Without the cache, most of our runtime in the cached case is `stat`'ing the same headers repeatedly.
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:  # The file doesn't exist or is inaccessible.
        # For our purposes, this means we don't have a newer version, so we'll return a very old time that'll always qualify the cache as fresh in a comparison. There are two cases here:
            # (1) Somehow it wasn't generated in the build that created the depfile. We therefore won't get any fresher by building, so we'll treat that as good enough; or
            # (2) It has been deleted since we last cached, in which case we'd rather use the cached version if it's otherwise fresh.
        return 0

    # Bazel internal sources have timestamps 10 years in the future as part of a mechanism to detect and prevent modification, so we'll similarly ignore those, since they shouldn't be changing.
    if mtime > BAZEL_INTERNAL_SOURCE_CUTOFF:
        return 0

    return mtime
# Roughly 1 year into the future. This is safely below bazel's 10 year margin, but large enough that no sane normal file should be past this.
BAZEL_INTERNAL_SOURCE_CUTOFF = time.time() + 60*60*24*365


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

    header_search_process = _subprocess_run_spilling_over_to_param_file_if_needed( # Note: gcc/clang can be run from Windows, too.
        header_cmd,
        # MIN_PY=3.7: Replace PIPEs with capture_output.
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding=locale.getpreferredencoding(),
        check=False, # We explicitly ignore errors and carry on.
    )

    # Tolerate failure gracefully--during editing the code may not compile!
    if header_search_process.stderr:
        _print_header_finding_warning_once()
        print(header_search_process.stderr, file=sys.stderr, end='') # Stderr captured and dumped atomically to avoid interlaced output.

    if not header_search_process.stdout: # Worst case, we couldn't get the headers,
        return set()
    # But often, we can get the headers, despite the error.

    return _parse_headers_from_makefile_deps(header_search_process.stdout)


@functools.lru_cache(maxsize=None)
def _get_clang_or_gcc():
    """Returns clang or gcc, if you have one of them on your path."""
    if shutil.which('clang'):
        return 'clang'
    elif shutil.which('gcc'):
        return 'gcc'
    else:
        return None


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


def _subprocess_run_spilling_over_to_param_file_if_needed(command: typing.List[str], **kwargs):
    """Same as subprocess.run, but it handles the case where the command line length is exceeded on Windows and we need a param file."""

    # On non-Windows, we have to run directly via a special case.
    # Otherwise, any exceptions will also trigger a NameError, since WindowsError is not defined outside of Windows.
    if os.name != 'nt':
        return subprocess.run(command, **kwargs)

    # See https://docs.microsoft.com/en-us/troubleshoot/windows-client/shell-experience/command-line-string-limitation
    try:
        return subprocess.run(command, **kwargs)
    except WindowsError as e:
        # We handle the error instead of calculating the command length because the length includes escaping internal to the subprocess.run call
        if e.winerror == 206:  # Thrown when command is too long, despite the error message being "The filename or extension is too long". For a few more details see also https://stackoverflow.com/questions/2381241/what-is-the-subprocess-popen-max-length-of-the-args-parameter
            # Write command to a temporary file, so we can use it as a parameter file to the compiler.
            # E.g. cl.exe @params_file.txt
            # tempfile.NamedTemporaryFile doesn't work because cl.exe can't open it--as the Python docs would indicate--so we have to do cleanup ourselves.
            fd, path = tempfile.mkstemp(text=True)
            try:
                os.write(fd, windows_list2cmdline(command[1:]).encode()) # should skip cl.exe the 1st line.
                os.close(fd)
                return subprocess.run([command[0], f'@{path}'], **kwargs)
            finally: # Safe cleanup even in the event of an error
                os.remove(path)
        else: # Some other WindowsError we didn't mean to catch.
            raise


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

    header_search_process = _subprocess_run_spilling_over_to_param_file_if_needed(
        header_cmd,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        env=environment,
        encoding=locale.getpreferredencoding(),
        check=False, # We explicitly ignore errors and carry on.
    )

    # Based on the locale, `cl.exe` will emit different marker strings. See also https://github.com/ninja-build/ninja/issues/613#issuecomment-885185024 and https://github.com/bazelbuild/bazel/pull/7966.
    # We can't just set environment['VSLANG'] = "1033" (English) and be done with it, because we can't assume the user has the English language pack installed.
    # Note that, if we're ever having problems with MSVC changing these strings too often, we can instead infer them by compiling some test files and passing /nologo. See https://github.com/ninja-build/ninja/issues/613#issuecomment-1465084387
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
    """Determine if one path is relative to another."""
    # MIN_PY=3.9: Eliminate helper in favor of `PurePath.is_relative_to()`.
    try:
        sub.relative_to(parent)
    except ValueError:
        return False
    return True


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
    elif {exclude_headers} == "external" and not {exclude_external_sources} and compile_action.is_external:
        # Shortcut - an external action can't include headers in the workspace (or, non-external headers)
        # The `not {exclude_external_sources}`` clause makes sure is_external was precomputed; there are no external actions if they've already been filtered in the process of excluding external sources.
        return set()

    output_file = None
    for i, arg in enumerate(compile_action.arguments):
        # As a reference, clang docs: https://clang.llvm.org/docs/ClangCommandLineReference.html#cmdoption-clang1-o-file
        if arg == '-o' or arg == '--output': # clang/gcc. Docs https://clang.llvm.org/docs/ClangCommandLineReference.html
            output_file = compile_action.arguments[i+1]
            break
        elif arg.startswith('/Fo') or arg.startswith('-Fo'): # MSVC *and clang*. MSVC docs https://docs.microsoft.com/en-us/cpp/build/reference/compiler-options-listed-alphabetically
            output_file = arg[3:]
            break
        elif arg.startswith('--output='):
            output_file = arg[9:]
            break
    # Since our output file parsing isn't complete, fall back on a warning message to solicit help.
    # A more full (if more involved) solution would be to get the primaryOutput for the action from the aquery output, but this should handle the cases Bazel emits.
    if not output_file and not _get_headers.has_logged:
        _get_headers.has_logged = True
        log_warning(f""">>> Please file an issue containing the following: Output file not detected in arguments {compile_action.arguments}.
    Not a big deal; things will work but will be a little slower.
    Thanks for your help!
    Continuing gracefully...""")

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
                    log_warning(f""">>> Ignoring corrupted header cache {cache_file_path}
    This is okay if you manually killed this tool earlier.
    But if this message is appearing spontaneously or frequently, please file an issue containing the contents of the corrupted cache, below.
    {cache_file.read()}
    Thanks for your help!
    Continuing gracefully...""")
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
        # Emscripten is tricky. There isn't an easy way to make it emcc run without lots of environment variables.
        # So...rather than doing our usual script unwrapping, we just swap in clang/gcc and use that to get headers, knowing that they'll accept the same argument format.
            # You can unwrap emcc.sh to emcc.py via next(pathlib.Path('external').glob('emscripten_bin_*/emscripten/emcc.py')).as_posix()
            # But then the underlying emcc needs a configuration file that itself depends on lots of environment variables.
            # If we ever pick this back up, note that you can supply that config via compile_args += ["--em-config", "external/emsdk/emscripten_toolchain/emscripten_config"]
        args = compile_action.arguments
        if args[0].endswith('emcc.sh') or args[0].endswith('emcc.bat'):
            alternate_compiler = _get_clang_or_gcc()
            if not alternate_compiler: return set() # Skip getting headers.
            args = args.copy()
            args[0] = alternate_compiler
        headers = _get_headers_gcc(args, source_path, compile_action.actionKey)

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

    # Getting the source file is a little trickier than it might seem.

    # First, we do the obvious thing: Filter args to those that look like source files.
    source_file_candidates = [arg for arg in compile_action.arguments if not arg.startswith('-') and arg.endswith(_get_files.source_extensions)]
    assert source_file_candidates, f"No source files found in compile args: {compile_action.arguments}.\nPlease file an issue with this information!"
    source_file = source_file_candidates[0]

    # If we've got multiple candidates for source files, apply heuristics based on how Bazel tends to format commands.
    # Note: Bazel, with its incremental building strategy, should only be compiling one source file per action.
    if len(source_file_candidates) > 1:
        # How does this case arise? Sometimes header search directories have source-file extensions. Horrible, but unfortunately true. See https://github.com/hedronvision/bazel-compile-commands-extractor/pull/37 for context and history.
        # You can't simply further filter the args to those that aren't existing directories...because they can be generated directories that don't yet exist. Indeed the example in the PR (linked above) is this case.
        # Bazel seems to consistently put the source file being compiled either:
            # before the -o flag, for GCC-formatted commands, or
            # after the /c flag, for MSVC-formatted commands
            # [See https://github.com/hedronvision/bazel-compile-commands-extractor/pull/72 for -c counterexample for GCC]
        # This is a strong assumption about Bazel internals, so we're taking some care to check that this condition holds with asserts. That way things are less likely to fail silently if it changes some day.
            # You can definitely have a proper invocation to clang/gcc/msvc where these assumptions don't hold.
        # However, parsing the command line this way is our best simple option. The other alternatives seem worse:
            # Parsing the clang invocation properly to get the positional file arguments is hard and not future-proof if new flags are added. Consider a new flag -foo. Does it also capture the next argument after it?
            # You might be tempted to crawl the inputs depset in the aquery output structure, but it's a fair amount of recursive code and there are other erroneous source files there, at least when building for Android in Bazel 5.1. You could fix this by intersecting the set of source files in the inputs with those listed as arguments on the command line, but I can imagine perverse, problematic cases here. It's a lot more code to still have those caveats.
            # You might be tempted to get the source files out of the action message listed (just) in aquery --output=text  output, but the message differs for external workspaces and tools. Plus paths with spaces are going to be hard because it's space delimited. You'd have to make even stronger assumptions than the -c.
                # Concretely, the message usually has the form "action 'Compiling foo.cpp'"" -> foo.cpp. But it also has "action 'Compiling src/tools/launcher/dummy.cc [for tool]'" -> external/bazel_tools/src/tools/launcher/dummy.cc
                # If we did ever go this route, you can join the output from aquery --output=text and --output=jsonproto by actionKey.
        if '-o' in compile_action.arguments: # GCC, pre -o case
            source_index = compile_action.arguments.index('-o') - 1
        else: # MSVC, post /C case
            assert '/c' in compile_action.arguments, f"-o or /c, required for parsing sources in GCC or MSVC-formatted commands, respectively, not found in compile args: {compile_action.arguments}.\nPlease file an issue with this information!"
            source_index = compile_action.arguments.index('/c') + 1

        source_file = compile_action.arguments[source_index]
        assert source_file.endswith(_get_files.source_extensions), f"Source file candidate, {source_file}, seems to be wrong.\nSelected from {compile_action.arguments}.\nPlease file an issue with this information!"

    # Warn gently about missing files
    if not os.path.isfile(source_file):
        if not _get_files.has_logged_missing_file_error: # Just log once; subsequent messages wouldn't add anything.
            _get_files.has_logged_missing_file_error = True
            log_warning(f""">>> A source file you compile doesn't (yet) exist: {source_file}
    It's probably a generated file, and you haven't yet run a build to generate it.
    That's OK; your code doesn't even have to compile for this tool to work.
    If you can, though, you might want to run a build of your code with --keep_going.
        That way everything possible is generated, browsable and indexed for autocomplete.
    However, if you have *already* built your code, and generated the missing file...
        Please make sure you're supplying this tool with the same flags you use to build.
        You can either use a refresh_compile_commands rule or the special -- syntax. Please see the README.
        [Supplying flags normally won't work. That just causes this tool to be built with those flags.]
    Continuing gracefully...""")
        return {source_file}, set()

    # Note: We need to apply commands to headers and sources.
    # Why? clangd currently tries to infer commands for headers using files with similar paths. This often works really poorly for header-only libraries. The commands should instead have been inferred from the source files using those libraries... See https://github.com/clangd/clangd/issues/123 for more.
    # When that issue is resolved, we can stop looking for headers and just return the single source file.

    # Assembly sources that are not preprocessed can't include headers
    if os.path.splitext(source_file)[1] in _get_files.assembly_source_extensions:
        return {source_file}, set()

    header_files = _get_headers(compile_action, source_file)

    # Ambiguous .h headers need a language specified if they aren't C, or clangd sometimes makes mistakes
    # Delete this and unused extension variables when clangd >= 16 is released, since their underlying issues are resolved at HEAD
    # Reference issues:
    # https://github.com/clangd/clangd/issues/1173
    # https://github.com/clangd/clangd/issues/1263
    if (any(header_file.endswith('.h') for header_file in header_files)
        and not source_file.endswith(_get_files.c_source_extensions)
        and not any(arg.startswith('-x') or arg.startswith('--language') or arg.lower() in ('-objc', '-objc++', '/tc', '/tp') for arg in compile_action.arguments)):
        if compile_action.arguments[0].endswith('cl.exe'): # cl.exe and also clang-cl.exe
            lang_flag = '/TP' # https://docs.microsoft.com/en-us/cpp/build/reference/tc-tp-tc-tp-specify-source-file-type
        else:
            lang_flag = _get_files.extensions_to_language_args[os.path.splitext(source_file)[1]]
        # Insert at front of (non executable) args, because --language is only supposed to take effect on files listed thereafter
        compile_action.arguments.insert(1, lang_flag)

    return {source_file}, header_files
_get_files.has_logged_missing_file_error = False
# Setup extensions and flags for the whole C-language family.
# Clang has a list: https://github.com/llvm/llvm-project/blob/b9f3b7f89a4cb4cf541b7116d9389c73690f78fa/clang/lib/Driver/Types.cpp#L293
_get_files.c_source_extensions = ('.c', '.i')
_get_files.cpp_source_extensions = ('.cc', '.cpp', '.cxx', '.c++', '.C', '.CC', '.cp', '.CPP', '.C++', '.CXX', '.ii')
_get_files.objc_source_extensions = ('.m',)
_get_files.objcpp_source_extensions = ('.mm', '.M')
_get_files.cuda_source_extensions = ('.cu', '.cui')
_get_files.opencl_source_extensions = ('.cl',)
_get_files.openclxx_source_extensions = ('.clcpp',)
_get_files.assembly_source_extensions = ('.s', '.asm')
_get_files.assembly_needing_c_preprocessor_source_extensions = ('.S',)
_get_files.source_extensions = _get_files.c_source_extensions + _get_files.cpp_source_extensions + _get_files.objc_source_extensions + _get_files.objcpp_source_extensions + _get_files.cuda_source_extensions + _get_files.opencl_source_extensions + _get_files.openclxx_source_extensions + _get_files.assembly_source_extensions + _get_files.assembly_needing_c_preprocessor_source_extensions
_get_files.extensions_to_language_args = { # Note that clangd fails on the --language or -ObjC or -ObjC++ forms. See https://github.com/clangd/clangd/issues/1173#issuecomment-1226847416
    _get_files.c_source_extensions: '-xc',
    _get_files.cpp_source_extensions: '-xc++',
    _get_files.objc_source_extensions: '-xobjective-c',
    _get_files.objcpp_source_extensions: '-xobjective-c++',
    _get_files.cuda_source_extensions: '-xcuda',
    _get_files.opencl_source_extensions: '-xcl',
    _get_files.openclxx_source_extensions: '-xclcpp',
    _get_files.assembly_source_extensions: '-xassembler',
    _get_files.assembly_needing_c_preprocessor_source_extensions: '-xassembler-with-cpp',
}
_get_files.extensions_to_language_args = {ext : flag for exts, flag in _get_files.extensions_to_language_args.items() for ext in exts} # Flatten map for easier use


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


def _apple_platform_patch(compile_args: typing.List[str]):
    """De-Bazel the command into something clangd can parse.

    This function has fixes specific to Apple platforms, but you should call it on all platforms. It'll determine whether the fixes should be applied or not.
    """
    # Bazel internal environment variable fragment that distinguishes Apple platforms that need unwrapping.
        # Note that this occurs in the Xcode-installed wrapper, but not the CommandLineTools wrapper, which works fine as is.
    if any('__BAZEL_XCODE_' in arg for arg in compile_args):
        # Undo Bazel's Apple platform compiler wrapping.
        # Bazel wraps the compiler as `external/local_config_cc/wrapped_clang` and exports that wrapped compiler in the proto. However, we need a clang call that clangd can introspect. (See notes in "how clangd uses compile_commands.json" in ImplementationReadme.md for more.)
        # Removing the wrapper is also important because Bazel's Xcode (but not CommandLineTools) wrapper crashes if you don't specify particular environment variables (replaced below). We'd need the wrapper to be invokable by clangd's --query-driver if we didn't remove the wrapper.
        compile_args[0] = 'clang'

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
    # We'll remove this flag, until such time as clangd & clang-tidy gracefully ignore it. Tracking issues: https://github.com/clangd/clangd/issues/1004 and https://github.com/llvm/llvm-project/issues/61699.
    # For more context see: https://github.com/hedronvision/bazel-compile-commands-extractor/issues/21
    compile_args = (arg for arg in compile_args if not arg == '-fno-canonical-system-headers')

    # Swap -isysroot for --sysroot to work around some unknown sysroot bug in clangd.
    # For context, see https://github.com/hedronvision/bazel-compile-commands-extractor/issues/82
    # The = logic has to do with clang not accepting -isysroot=, but accepting --sysroot=. Note that -isysroot <path> is accepted, though undocumented.
    compile_args = ('-isysroot'+arg[len('--sysroot')+arg.startswith('--sysroot='):] if arg.startswith('--sysroot') else arg for arg in compile_args)

    # Strip out -gcc-toolchain to work around https://github.com/clangd/clangd/issues/1248
    skip_next = False
    new_compile_args = []
    for arg in compile_args:
        if arg.startswith('-gcc-toolchain'):
            if len(arg) == len('-gcc-toolchain'):
                skip_next = True
        elif skip_next:
            skip_next = False
        else:
            new_compile_args.append(arg)
    compile_args = new_compile_args

    # Any other general fixes would go here...

    return compile_args


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
    if {exclude_headers} == "external" and not {exclude_external_sources}:
        targets_by_id = {target.id : target.label for target in aquery_output.targets}
        for action in aquery_output.actions:
            # Tag action as external if it's created by an external target
            target = targets_by_id[action.targetId] # Should always be present. KeyError as implicit assert.
            assert not target.startswith('//external'), f"Expecting external targets will start with @. Found //external for action {action}, target {target}"
            action.is_external = target.startswith('@') and not target.startswith('@//')

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
            if file == 'external/bazel_tools/src/tools/launcher/dummy.cc': continue # Suppress Bazel internal files leaking through. Hopefully will prevent issues like https://github.com/hedronvision/bazel-compile-commands-extractor/issues/77
            yield {
                # Docs about compile_commands.json format: https://clang.llvm.org/docs/JSONCompilationDatabase.html#format
                'file': file,
                # Using `arguments' instead of 'command' because it's now preferred by clangd. Heads also that  shlex.join doesn't work for windows cmd, so you'd need to use windows_list2cmdline if we ever switched back. For more, see https://github.com/hedronvision/bazel-compile-commands-extractor/issues/8#issuecomment-1090262263
                'arguments': compile_command_args,
                # Bazel gotcha warning: If you were tempted to use `bazel info execution_root` as the build working directory for compile_commands...search ImplementationReadme.md to learn why that breaks.
                'directory': os.environ["BUILD_WORKSPACE_DIRECTORY"],
            }


def _get_commands(target: str, flags: str):
    """Yields compile_commands.json entries for a given target and flags, gracefully tolerating errors."""
    # Log clear completion messages
    log_info(f">>> Analyzing commands used in {target}")

    additional_flags = shlex.split(flags) + sys.argv[1:]

    # Detect anything that looks like a build target in the flags, and issue a warning.
    # Note that positional arguments after -- are all interpreted as target patterns. (If it's at the end, then no worries.)
    # And that we have to look for targets. checking for a - prefix is not enough. Consider the case of `-c opt` leading to a false positive
    if ('--' in additional_flags[:-1]
        or any(re.match(r'-?(@|:|//)', f) for f in additional_flags)):
        log_warning(""">>> The flags you passed seem to contain targets.
    Try adding them as targets in your refresh_compile_commands rather than flags.
    [Specifying targets at runtime isn't supported yet, and in a moment, Bazel will likely fail to parse without our help. If you need to be able to specify targets at runtime, and can't easily just add them to your refresh_compile_commands, please open an issue or file a PR. You may also want to refer to https://github.com/hedronvision/bazel-compile-commands-extractor/issues/62.]""")

    # Quick (imperfect) effort at detecting flags in the targets.
    # Can't detect flags starting with -, because they could be subtraction patterns.
    if any(target.startswith('--') for target in shlex.split(target)):
        log_warning(""">>> The target you specified seems to contain flags.
    Try adding them as flags in your refresh_compile_commands rather than targets.
    In a moment, Bazel will likely fail to parse.""")

    # First, query Bazel's C-family compile actions for that configured target
    target_statment = f'deps({target})'
    if {exclude_external_sources}:
        # For efficiency, have bazel filter out external targets (and therefore actions) before they even get turned into actions or serialized and sent to us. Note: this is a different mechanism than is used for excluding just external headers.
        target_statment = f"filter('^(//|@//)',{target_statment})"
    aquery_args = [
        'bazel',
        'aquery',
        # Aquery docs if you need em: https://docs.bazel.build/versions/master/aquery.html
        # Aquery output proto reference: https://github.com/bazelbuild/bazel/blob/master/src/main/protobuf/analysis_v2.proto
        # One bummer, not described in the docs, is that aquery filters over *all* actions for a given target, rather than just those that would be run by a build to produce a given output. This mostly isn't a problem, but can sometimes surface extra, unnecessary, misconfigured actions. Chris has emailed the authors to discuss and filed an issue so anyone reading this could track it: https://github.com/bazelbuild/bazel/issues/14156.
        f"mnemonic('(Objc|Cpp)Compile',{target_statment})",
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
        # Disable layering_check during, because it causes large-scale dependence on generated module map files that prevent header extraction before their generation
            # For more context, see https://github.com/hedronvision/bazel-compile-commands-extractor/issues/83
            # If https://github.com/clangd/clangd/issues/123 is resolved and we're not doing header extraction, we could try removing this, checking that there aren't erroneous red squigglies squigglies before the module maps are generated.
            # If Bazel starts supporting modules (https://github.com/bazelbuild/bazel/issues/4005), we'll probably need to make changes that subsume this.
        '--features=-layering_check',
    ] + additional_flags

    aquery_process = subprocess.run(
        aquery_args,
        # MIN_PY=3.7: Replace PIPEs with capture_output.
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding=locale.getpreferredencoding(),
        check=False, # We explicitly ignore errors from `bazel aquery` and carry on.
    )


    # Filter aquery error messages to just those the user should care about.
    # Shush known warnings about missing graph targets.
    # The missing graph targets are not things we want to introspect anyway.
    # Tracking issue https://github.com/bazelbuild/bazel/issues/13007
    missing_targets_warning: typing.Pattern[str] = re.compile(r'(\(\d+:\d+:\d+\) )?(\033\[[\d;]+m)?WARNING: (\033\[[\d;]+m)?Targets were missing from graph:') # Regex handles --show_timestamps and --color=yes. Could use "in" if we ever need more flexibility.
    aquery_process.stderr = '\n'.join(line for line in aquery_process.stderr.splitlines() if not missing_targets_warning.match(line))
    if aquery_process.stderr: print(aquery_process.stderr, file=sys.stderr)

    # Parse proto output from aquery
    try:
        # object_hook -> SimpleNamespace allows object.member syntax, like a proto, while avoiding the protobuf dependency
        parsed_aquery_output = json.loads(aquery_process.stdout, object_hook=lambda d: types.SimpleNamespace(**d))
    except json.JSONDecodeError:
        print("Bazel aquery failed. Command:", aquery_args, file=sys.stderr)
        log_warning(f">>> Failed extracting commands for {target}\n    Continuing gracefully...")
        return

    if not getattr(parsed_aquery_output, 'actions', None): # Unifies cases: No actions (or actions list is empty)
        if aquery_process.stderr:
            log_warning(f""">>> Bazel lists no applicable compile commands for {target}, probably because of errors in your BUILD files, printed above.
    Continuing gracefully...""")
        else:
            log_warning(f""">>> Bazel lists no applicable compile commands for {target}
    If this is a header-only library, please instead specify a test or binary target that compiles it (search "header-only" in README.md).
    Continuing gracefully...""")
        return

    yield from _convert_compile_commands(parsed_aquery_output)


    # Log clear completion messages
    log_success(f">>> Finished extracting commands for {target}")


def _ensure_external_workspaces_link_exists():
    """Postcondition: Either //external points into Bazel's fullest set of external workspaces in output_base, or we've exited with an error that'll help the user resolve the issue."""
    is_windows = os.name == 'nt'
    source = pathlib.Path('external')

    if not os.path.lexists('bazel-out'):
        log_error(">>> //bazel-out is missing. Please remove --symlink_prefix and --experimental_convenience_symlinks, so the workspace mirrors the compilation environment.")
        # Crossref: https://github.com/hedronvision/bazel-compile-commands-extractor/issues/14 https://github.com/hedronvision/bazel-compile-commands-extractor/pull/65
        # Note: experimental_no_product_name_out_symlink is now enabled by default. See https://github.com/bazelbuild/bazel/commit/06bd3e8c0cd390f077303be682e9dec7baf17af2
        sys.exit(1)

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
            log_error(f">>> //external already exists, but it isn't a {'junction' if is_windows else 'symlink'}. //external is reserved by Bazel and needed for this tool. Please rename or delete your existing //external and rerun. More details in the README if you want them.") # Don't auto delete in case the user has something important there.
            sys.exit(1)

        # Normalize the path for matching
        # First, workaround a gross case where Windows readlink returns extended path, starting with \\?\, causing the match to fail
        if is_windows and current_dest.startswith('\\\\?\\'):
            current_dest = current_dest[4:] # MIN_PY=3.9 stripprefix
        current_dest = pathlib.Path(current_dest)

        if dest != current_dest:
            log_warning(">>> //external links to the wrong place. Automatically deleting and relinking...")
            source.unlink()

    # Create link if it doesn't already exist
    if not os.path.lexists(source):
        if is_windows:
            # We create a junction on Windows because symlinks need more than default permissions (ugh). Without an elevated prompt or a system in developer mode, symlinking would fail with get "OSError: [WinError 1314] A required privilege is not held by the client:"
            subprocess.run(f'mklink /J "{source}" "{dest}"', check=True, shell=True) # shell required for mklink builtin
        else:
            source.symlink_to(dest, target_is_directory=True)
        log_success(""">>> Automatically added //external workspace link:
    This link makes it easy for you--and for build tooling--to see the external dependencies you bring in. It also makes your source tree have the same directory structure as the build sandbox.
    It's a win/win: It's easier for you to browse the code you use, and it eliminates whole categories of edge cases for build tooling.""")


def _ensure_gitignore_entries_exist():
    """Ensure `//compile_commands.json`, `//external`, and other useful entries are `.gitignore`'d if in a git repo."""
    # Silently check if we're (nested) within a git repository. It isn't sufficient to check for the presence of a `.git` directory, in case, e.g., the bazel workspace is nested inside the git repository or you're off in a git worktree.
    git_dir_process = subprocess.run('git rev-parse --git-common-dir', # common-dir because despite current gitignore docs, there's just one info/exclude in the common git dir, not one in each of the worktree's git dirs.
        shell=True,  # Ensure this will still fail with a nonzero error code even if `git` isn't installed, unifying error cases.
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        encoding=locale.getpreferredencoding(),
    )
    # A nonzero error code indicates that we are not (nested) within a git repository.
    if git_dir_process.returncode: return

    # Write into the gitignore hidden inside the .git directory
    # This makes ignoring work automagically for people, while minimizing the code changes they have to think about or check in. https://github.com/hedronvision/bazel-compile-commands-extractor/pull/100 and https://github.com/hedronvision/bazel-compile-commands-extractor/issues/59 are exampels of use cases that this simplifies. It also marginally simplifies the case where people can't commit use of this tool to the repo they're working on.
    # IMO tools should to do this more broadly, especially now that git is so dominant.
    # Hidden gitignore documented in https://git-scm.com/docs/gitignore
    git_dir = pathlib.Path(git_dir_process.stdout.rstrip())
    (git_dir / 'info').mkdir(exist_ok=True) # Some older git versions don't auto create .git/info/, creating an error on exclude file open. See https://github.com/hedronvision/bazel-compile-commands-extractor/issues/114 for more context. We'll create the .git/info/ if needed; the git docs don't guarantee its existance. (We could instead back to writing .gitignore in the repo and bazel workspace, but we don't because this case is rare and because future git versions would be within their rights to read .git/info/exclude but not auto-create .git/info/)
    hidden_gitignore_path = git_dir / 'info' / 'exclude'

    # Get path to the workspace root (current working directory) from the git repository root
    git_prefix_process = subprocess.run(['git', 'rev-parse', '--show-prefix'],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        encoding=locale.getpreferredencoding(),
        check=True, # Should always succeed if the other did
    )
    pattern_prefix = git_prefix_process.stdout.rstrip()

    # Each (pattern, explanation) will be added to the `.gitignore` file if the pattern isn't present.
    needed_entries = [
        (f'/{pattern_prefix}external', "# Ignore the `external` link (that is added by `bazel-compile-commands-extractor`). The link differs between macOS/Linux and Windows, so it shouldn't be checked in. The pattern must not end with a trailing `/` because it's a symlink on macOS/Linux."),
        (f'/{pattern_prefix}bazel-*', "# Ignore links to Bazel's output. The pattern needs the `*` because people can change the name of the directory into which your repository is cloned (changing the `bazel-<workspace_name>` symlink), and must not end with a trailing `/` because it's a symlink on macOS/Linux. This ignore pattern should almost certainly be checked into a .gitignore in your workspace root, too, for folks who don't use this tool."),
        (f'/{pattern_prefix}compile_commands.json', "# Ignore generated output. Although valuable (after all, the primary purpose of `bazel-compile-commands-extractor` is to produce `compile_commands.json`!), it should not be checked in."),
        ('.cache/', "# Ignore the directory in which `clangd` stores its local index."),
    ]

    # Create `.gitignore` if it doesn't exist (and don't truncate if it does) and open it for appending/updating.
    with open(hidden_gitignore_path, 'a+') as gitignore:
        gitignore.seek(0)  # Files opened in `a` mode seek to the end, so we reset to the beginning so we can read.
        # Recall that trailing spaces, when escaped with `\`, are meaningful to git. However, none of the entries for which we're searching end with literal spaces, so we can safely trim all trailing whitespace. That said, we can't rewrite these stripped lines to the file, in case an existing entry is e.g. `/foo\ `, matching the file "foo " (with a trailing space), whereas the entry `/foo\` does not match the file `"foo "`.
        lines = [l.rstrip() for l in gitignore]
        # Comments must be on their own line, so we can safely check for equality here.
        missing = [entry for entry in needed_entries if entry[0] not in lines]
        if not missing:
            return
        # Add a spacer before the header if the last line is nonempty.
        if lines and lines[-1]:
            print(file=gitignore)
        # Add a nice header.
        print("### Automatically added by Hedron's Bazel Compile Commands Extractor: https://github.com/hedronvision/bazel-compile-commands-extractor", file=gitignore)
        # Append the missing entries.
        for pattern, comment in missing:
            print(comment, file=gitignore)
            print(pattern, file=gitignore)
    log_success(">>> Automatically added entries to .git/info/exclude to gitignore generated output.")


def _ensure_cwd_is_workspace_root():
    """Set the current working directory to the root of the workspace."""
    # The `bazel run` command sets `BUILD_WORKSPACE_DIRECTORY` to "the root of the workspace where the build was run." See: https://bazel.build/docs/user-manual#running-executables.
    try:
        workspace_root = pathlib.Path(os.environ['BUILD_WORKSPACE_DIRECTORY'])
    except KeyError:
        log_error(">>> BUILD_WORKSPACE_DIRECTORY was not found in the environment. Make sure to invoke this tool with `bazel run`.")
        sys.exit(1)
    # Change the working directory to the workspace root (assumed by future commands).
    # Although this can fail (OSError/FileNotFoundError/PermissionError/NotADirectoryError), there's no easy way to recover, so we'll happily crash.
    os.chdir(workspace_root)


def main():
    _ensure_cwd_is_workspace_root()
    _ensure_gitignore_entries_exist()
    _ensure_external_workspaces_link_exists()

    target_flag_pairs = [
        # Begin: template filled by Bazel
        {target_flag_pairs}
        # End:   template filled by Bazel
    ]

    compile_command_entries = []
    for (target, flags) in target_flag_pairs:
        compile_command_entries.extend(_get_commands(target, flags))

    if not compile_command_entries:
        log_error(""">>> Not (over)writing compile_commands.json, since no commands were extracted and an empty file is of no use.
    There should be actionable warnings, above, that led to this.""")
        sys.exit(1)

    # Chain output into compile_commands.json
    with open('compile_commands.json', 'w') as output_file:
        json.dump(
            compile_command_entries,
            output_file,
            indent=2, # Yay, human readability!
            check_circular=False # For speed.
        )
