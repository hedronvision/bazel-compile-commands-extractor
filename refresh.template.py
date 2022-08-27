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
import shlex
import subprocess
import time
import types
import typing # MIN_PY=3.9: Switch e.g. typing.List[str] -> list[str]

from command_reformatter import CommandReformatter

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


def _convert_compile_commands(aquery_output):
    """Converts from Bazel's aquery format to de-Bazeled compile_commands.json entries.

    Input: jsonproto output from aquery, pre-filtered to (Objective-)C(++) compile actions for a given build.
    Yields: Corresponding entries for a compile_commands.json, with commas after each entry, describing all ways every file is being compiled.
        Also includes one entry per header, describing one way it is compiled (to work around https://github.com/clangd/clangd/issues/123).

    Crucially, this de-Bazels the compile commands it takes as input, leaving something clangd can understand. The result is a command that could be run from the workspace root directly, with no bazel-specific environment variables, etc.
    """

    windows_default_include_paths = (
        # Begin: template filled by Bazel
        {windows_default_include_paths}
        # End:   template filled by Bazel
    )

    reformatter = CommandReformatter({exclude_headers},
                                     windows_default_include_paths,
                                     _print_header_finding_warning_once,
                                     _get_bazel_cached_action_keys,
                                     _get_cached_adjusted_modified_time,
                                     _get_apple_SDKROOT,
                                     _get_apple_DEVELOPER_DIR,
                                     _get_apple_active_clang)

    def worker(compile_action):
        return reformatter.reformat(compile_action)

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
        outputs = threadpool.map(worker, aquery_output.actions)

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
        print("\033[0;31m>>> //bazel-out is missing. Please remove --symlink_prefix and --experimental_convenience_symlinks, so the workspace mirrors the compilation environment.\033[0m", file=sys.stderr)
        # Crossref: https://github.com/hedronvision/bazel-compile-commands-extractor/issues/14 https://github.com/hedronvision/bazel-compile-commands-extractor/pull/65
        # Note: experimental_no_product_name_out_symlink is now enabled by default. See https://github.com/bazelbuild/bazel/commit/06bd3e8c0cd390f077303be682e9dec7baf17af2
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
