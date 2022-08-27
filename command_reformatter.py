import sys
import json
import locale
import os
import pathlib
import re
import subprocess
import tempfile
import typing # MIN_PY=3.9: Switch e.g. typing.List[str] -> list[str]


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


class CommandReformatter:
    def __init__(self, exclude_headers, windows_default_include_paths, _print_header_finding_warning_once, _get_bazel_cached_action_keys, _get_cached_adjusted_modified_time, _get_apple_SDKROOT, _get_apple_DEVELOPER_DIR, _get_apple_active_clang):
        self.exclude_headers = exclude_headers
        self.windows_default_include_paths = windows_default_include_paths
        self._print_header_finding_warning_once = _print_header_finding_warning_once
        self._get_bazel_cached_action_keys = _get_bazel_cached_action_keys
        self._get_cached_adjusted_modified_time = _get_cached_adjusted_modified_time
        self._get_apple_SDKROOT = _get_apple_SDKROOT
        self._get_apple_DEVELOPER_DIR = _get_apple_DEVELOPER_DIR
        self._get_apple_active_clang = _get_apple_active_clang
        self._get_headers_has_logged = False
        self._get_files_has_logged_missing_file_error = False


    def _get_headers_gcc(self, compile_args: typing.List[str], source_path: str, action_key: str):
        """Gets the headers used by a particular compile command that uses gcc arguments formatting (including clang.)

        Relatively slow. Requires running the C preprocessor if we can't hit Bazel's cache.
        """
        # Flags reference here: https://clang.llvm.org/docs/ClangCommandLineReference.html

        # Check to see if Bazel has an (approximately) fresh cache of the included headers, and if so, use them to avoid a slow preprocessing step.
        if action_key in self._get_bazel_cached_action_keys():  # Safe because Bazel only holds one cached action key per path, and the key contains the path.
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
                        if (self._get_cached_adjusted_modified_time(source_path) <= dep_file_last_modified
                                and all(self._get_cached_adjusted_modified_time(header_path) <= dep_file_last_modified for header_path in headers)):
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
            self._print_header_finding_warning_once()
            print(header_search_process.stderr, file=sys.stderr, end='') # Captured with capture_output and dumped explicitly to avoid interlaced output.

        if not header_search_process.stdout: # Worst case, we couldn't get the headers,
            return set()
        # But often, we can get the headers, despite the error.

        return _parse_headers_from_makefile_deps(header_search_process.stdout)


    def _get_headers_msvc(self, compile_args: typing.List[str], source_path: str):
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
        environment['INCLUDE'] = os.pathsep.join(self.windows_default_include_paths)

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
            self._print_header_finding_warning_once()
            print('\n'.join(error_lines), file=sys.stderr)

        return headers


    def _get_headers(self, compile_action, source_path: str):
        """Gets the headers used by a particular compile command.

        Relatively slow. Requires running the C preprocessor.
        """
        # Hacky, but hopefully this is a temporary workaround for the clangd issue mentioned in the caller (https://github.com/clangd/clangd/issues/123)
        # Runs a modified version of the compile command to piggyback on the compiler's preprocessing and header searching.

        # As an alternative approach, you might consider trying to get the headers by inspecting the Middlemen actions in the aquery output, but I don't see a way to get just the ones actually #included--or an easy way to get the system headers--without invoking the preprocessor's header search logic.
        # For more on this, see https://github.com/hedronvision/bazel-compile-commands-extractor/issues/5#issuecomment-1031148373

        if self.exclude_headers == "all":
            return set()
        elif self.exclude_headers == "external" and compile_action.is_external:
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
        if not output_file and not self._get_headers_has_logged:
            self._get_headers_has_logged = True
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
                            and self._get_cached_adjusted_modified_time(source_path) <= cache_last_modified
                            and all(self._get_cached_adjusted_modified_time(header_path) <= cache_last_modified for header_path in headers)):
                        return set(headers)

        if compile_action.arguments[0].endswith('cl.exe'): # cl.exe and also clang-cl.exe
            headers = self._get_headers_msvc(compile_action.arguments, source_path)
        else:
            headers = self._get_headers_gcc(compile_action.arguments, source_path, compile_action.actionKey)

        # Cache for future use
        if output_file:
            os.makedirs(os.path.dirname(cache_file_path), exist_ok=True)
            with open(cache_file_path, 'w') as cache_file:
                json.dump((compile_action.actionKey, list(headers)), cache_file)

        if self.exclude_headers == "external":
            headers = {header for header in headers if _file_is_in_main_workspace_and_not_external(header)}

        return headers


    def _get_files(self, compile_action):
        """Gets the ({source files}, {header files}) clangd should be told the command applies to."""

        # Getting the source file is a little trickier than it might seem.
        # Bazel seems to consistently put the source file being compiled either:
        # before the -o flag, for GCC-formatted commands, or
        # after the /c flag, for MSVC-formatted commands
        # [See https://github.com/hedronvision/bazel-compile-commands-extractor/pull/72 for -c counterexample for GCC]
        # This is a strong assumption about Bazel internals, so we're taking some care to check that this condition holds with asserts. That way things are less likely to fail silently if it changes some day.
        # You can definitely have a proper invocation to clang/gcc/msvc where these assumptions don't hold.
        # However, parsing the command line this way is our best simple option. The other alternatives seem worse:
        # You can't just filter the args to those that end with source-file extensions. The problem is that sometimes header search directories have source-file extensions. Horrible, but unfortunately true. See https://github.com/hedronvision/bazel-compile-commands-extractor/pull/37 for context and history.
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
        SOURCE_EXTENSIONS = ('.c', '.cc', '.cpp', '.cxx', '.c++', '.C', '.m', '.mm', '.cu', '.cl', '.s', '.asm', '.S')
        assert source_file.endswith(SOURCE_EXTENSIONS), f"Source file candidate, {source_file}, seems to be wrong.\nSelected from {compile_action.arguments}.\nPlease file an issue with this information!"

        # Warn gently about missing files
        file_exists = os.path.isfile(source_file)
        if not file_exists:
            if not self._get_files_has_logged_missing_file_error: # Just log once; subsequent messages wouldn't add anything.
                self._get_files_has_logged_missing_file_error = True
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
        return {source_file}, self._get_headers(compile_action, source_file) if file_exists else set()


    def _apple_platform_patch(self, compile_args: typing.List[str]):
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
            compile_args[0] = self._get_apple_active_clang()

            # We have to manually substitute out Bazel's macros so clang can parse the command
            # Code this mirrors is in https://github.com/bazelbuild/bazel/blob/master/tools/osx/crosstool/wrapped_clang.cc
            # Not complete--we're just swapping out the essentials, because there seems to be considerable turnover in the hacks they have in the wrapper.
            compile_args = [arg for arg in compile_args if not arg.startswith('DEBUG_PREFIX_MAP_PWD') or arg == 'OSO_PREFIX_MAP_PWD'] # No need for debug prefix maps if compiling in place, not that we're compiling anyway.
            # We also have to manually figure out the values of SDKROOT and DEVELOPER_DIR, since they're missing from the environment variables Bazel provides.
            # Filed Bazel issue about the missing environment variables: https://github.com/bazelbuild/bazel/issues/12852
            compile_args = [arg.replace('__BAZEL_XCODE_DEVELOPER_DIR__', self._get_apple_DEVELOPER_DIR()) for arg in compile_args]
            apple_platform = _get_apple_platform(compile_args)
            assert apple_platform, f"Apple platform not detected in CMD: {compile_args}"
            compile_args = [arg.replace('__BAZEL_XCODE_SDKROOT__', self._get_apple_SDKROOT(apple_platform)) for arg in compile_args]

        return compile_args


    def _all_platform_patch(self, compile_args: typing.List[str]):
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


    def reformat(self, compile_action):
        """Reformat compile_action into a compile command clangd can understand.

        Undo Bazel-isms and figures out which files clangd should apply the command to.
        """
        # Patch command by platform
        compile_action.arguments = self._all_platform_patch(compile_action.arguments)
        compile_action.arguments = self._apple_platform_patch(compile_action.arguments)
        # Android and Linux and grailbio LLVM toolchains: Fine as is; no special patching needed.

        source_files, header_files = self._get_files(compile_action)

        return source_files, header_files, compile_action.arguments
