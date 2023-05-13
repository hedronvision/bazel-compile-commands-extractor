# Hedron's Compile Commands Extractor for Bazel — Implementation

> Full Reading Time: 15m

Hello, and thanks for your interest in the codebase :)

**Goal:** *If you need to work under the hood, let's get you oriented efficiently.*

**Interface:** *Flattened information tree. Hop around to find what you need.*

## Setting Up Local Development

We'd recommend setting up for rapid iteration as follows:

Get a local copy. (e.g. `git clone git@github.com:hedronvision/bazel-compile-commands-extractor.git`)

Then point your main repo (from which you're seeing issues) to use your local development copy of the compile commands. To do that, open `WORKSPACE`, and swap out the `http_archive` loading `hedron_compile_commands` with the following.

```Starlark
local_repository(
    name = "hedron_compile_commands",
    path = "../bazel-compile-commands-extractor", # Or wherever you put it.
)
```

You should then be able to make local changes and see their effects immediately.

### Word Wrap

To edit this repository happily, you'll likely want to turn on word wrap in your editor. For example, in VSCode, Settings>Editor: Word Wrap>on (or bounded).

We use some fairly long lines and don't hard wrap at e.g. 80ch. The philosophy is that that these days, we're all trying to make the most of variable-width windows rather than the fixed-width terminals/punch cards of yore (punch cards being the origin of the 80ch convention!).

We'd appreciate your joining us in aiming for clarity and ease of expression--by using line breaks to separate ideas--but not to manually reimplement word wrapping.

### Cleanup Commits

If you see ways to make anything better, we'd really appreciate your help! Unlike other projects that discourage quality improvements, we'd love it if you'd propose changes to leave things even better than you found them.

## Overall Strategy

To get great autocomplete and enable other tooling, we need to get Bazel's understanding of how to compile the code into the `compile_commands.json` common format that `clangd`—and other good clang tooling—understands.

The `refresh_compile_commands` rule (from [`refresh_compile_commands.bzl`](./refresh_compile_commands.bzl)) drives the process. It constructs a `refresh.py` script from [`refresh.template.py`](./refresh.template.py) to be run for the targets you've chosen. That script drives the following actions.

1. We ask Bazel which compile commands it plans to issue during build actions using [`aquery` ("action query")](https://docs.bazel.build/versions/master/aquery.html).
2. We then reformat each of those into `compile_commands.json` entries clangd understands.
3. Clangd then works with VSCode to provide a nice editing experience.


## Code Layout

- [`refresh.template.py`](./refresh.template.py) is the main driver of actions. Browsing it should help you figure out where you want to go. It consists of two sections: one for calling `bazel aquery` and one for constructing the `compile_commands.json` from it. The latter does the actual reformatting of command so they're usable by `clangd`. This is more involved than you might think, but not crazy. It's easy to extend the reformatting operations applied.
  - If you're seeing failures on a new platform, weird entries in `compile_commands.json`, or wrapped compilers, this is where you should make changes to properly undo Bazel's wrapping of the command. See the "Patch command by platform" section.
- The other files ([`refresh_compile_commands.bzl`](./refresh_compile_commands.bzl) and others) are just wrappings. They're less likely to require your attention, except as directed from the README or refresh.template.py.


## Tool Choice — Why `clangd`?

[Clangd](https://clangd.llvm.org) was the clear choice for the language server for autocomplete and editing help.

Why?

- LLVM projects are well-built, best-poised to understand languages, and well-backed.
  - You want tooling that understands the language. Compiler front ends understand languages best. Tooling that builds on the best compiler frontend—and is written by the same folks—is likely to be best.
  - The community is great: `clangd` is very responsive on GitHub, it's integrated in many editors (including VSCode), and there are many deep pockets that need it and fund it.
- The alternatives have key drawbacks:
  - The Microsoft VSCode plugin for C++ doesn't support Objective-C (which we need), would more tightly couple us to VSCode with a non-standard compile commands format, doesn't seem to be based on a compiler, and has had various issues parsing platform command flags.
  - `ccls` is a valiant effort similar to clangd, but it's mostly one guy. I'd bet on the LLVM ecosystem, but it is really good and competitive at the moment. Previously, it was ahead of clangd on indexing and performance.
    - `cquery` is an older (abandoned) effort that `ccls` is a sequel to.


### Expansion opportunity into `clang-tidy`
`clangd` is also intimately connected to `clang-tidy` if we ever want that. `clang-tidy` via `clangd` can be configured via an [Options file](https://clangd.llvm.org/config.html). It looks like future settings may migrate from flags into that options file, but it's not worth using as of 2/2021; it doesn't have the config we need right now.


## compile_commands.json: Interface Between Build System and Tooling

Clangd (and most of these tools), use `compile_commands.json` ([spec](https://clang.llvm.org/docs/JSONCompilationDatabase.html), [additional description](https://sarcasm.github.io/notes/dev/compilation-database.html#bazel)).


### How `clangd` uses `compile_commands.json`

It's worth knowing that `clangd` doesn't actually execute the command, but rather swaps in its own command as the first token and then listens to the flags provided. (You might also have guessed instead, as I originally did, that `clangd` invoked the compiler as a dry-run and listened, but that's wrong.)

This means that fancy things like specifying environment variables in the command or passing in a wrapper compiler driver that accepts the same flags but does some unnecessary expansion (Bazel...) will break things. You need to carefully unwrap Bazel's (unnecessary) compiler driver wrappers to get clangd to pick up on commands.

`clangd` also tries to introspect the compiler specified to figure out what include paths it adds implicitly. Usually it just checks the relative path, following clang (and maybe others') conventions. Iff you use the `--query-driver` flag it will directly invoke the compiler and ask it about those includes. If you don't specify `--query-driver` and it can't find the includes at the relative path (like in the case of Bazel's compiler wrappers) it will miss those default includes. If you're seeing red squigglies under, e.g., standard library headers or system headers that should be included by default, you've probably run into a failure of this type. [[For more context, see this issue about making driver automatic.](https://github.com/clangd/clangd/issues/539)]

All this means it's crucial to de-Bazel the command we write to `compile_commands.json` enough that `clangd` can invoke it to get default header search paths--and so `clangd` can understand its flags *without* invoking it. No compiler driver wrappers requiring Bazel-specific environment variables to run, nor flags that need custom expansion `clangd` wouldn't know about, etc. All this happens in [`refresh.template.py`](./refresh.template.py), details there.

If you see warning messages like "compilation failed" and "index may be incomplete" for almost all entries in the `clangd` log (view in VSCode under Output>clangd), it's because `clangd` is misparsing the command in a way that breaks its ability to understand things. A few messages like this are fine; they come from (poorly-designed) headers that depend on include order. (See also note about this in https://github.com/hedronvision/bazel-compile-commands-extractor/issues/2].)

(This section summarizes learnings originally from the discussion in [this issue](https://github.com/clangd/clangd/issues/654) Chris filed, where a Googler working on LLVM was very helpful.)

### Our Output

We're using the simplest complete subset of `compile_commands.json` keys (command, file, directory), because it's easy, general, and gets us everything we need.

#### Compilation Working Directory and the //external Link

We make the choice to set the compilation working directory ("directory" key) to our Bazel workspace root (`bazel info workspace`), because it always contains *all* the source files we need, rather than a subset.

##### The Trap Being Avoided

I'm calling out this decision explicitly, because there's a tempting trap that caught Bazel's official editor plugins (Android Studio, Xcode, ...) before we helped fix them.

Do not be tempted to set the compilation "directory" to the bazel execroot (`bazel info execution_root`). The execroot may seem to work but breaks subtly on the next build; the execroot directory is reconfigured for whatever new target is being built, deleting the links to external workspaces and top-level packages not used by that particular build. Using the execroot might be tempting because that *is* where Bazel says it's going to invoke the command it gave you, but don't do it! It'll only have the subset of the code used for the last build, not for all build, breaking the paths used for editing the rest of the codebase.

Remember that the key goal of `compile_commands.json` is to "de-Bazel" the build commands into something `clangd` can understand. Not pointing into Bazel's temporary build scratch space (execroot) is an important part of decoupling from Bazel's transitory state.

##### Generated files: `//external` link makes external dependencies work

Having avoided the execroot trap, we have `compile_commands.json` describing compile commands directly in our workspace.

There are two other important cases to consider: generated files and external code. In each of these cases, we can't point to a bazel-independent source; Bazel generates the files! But we can choose to point into a cache where Bazel *accumulates* the files rather than the execroot build directory, where some disappear on each build.

For generated files, Bazel creates a nice link for us in our workspace, `//bazel-out` that points into the cache. This location accumulates the most recent build products for each platform (despite being in execroot.) Commands then just work because `//bazel-out` has the same name and relative path as the `bazel-out` in Bazel's compilation sandbox. Great!

For external code (that we've brought in via WORKSPACE), Bazel's build commands look for an `//external` directory in the workspace. Bazel doesn't create a link for it by default...so we created one, and everything works.

The `//external` link thus makes the Bazel workspace root an accurate reflection of all source files in the project, including external code and generated code, with paths the same as in Bazel's build sandbox. The `//external` link is also handy to be able to easily see what code Bazel has pulled in from the outside!

###### More details on `//external`

We auto-create the link with the equivalent of `ln -s bazel-out/../../../external .` in [`refresh.template.py`](./refresh.template.py)'s `_ensure_external_workspaces_link_exists`

This points into the accumulating cache under the output base, where external code is cached and accumulated. Crucially, it *doesn't* point to the temporary references to external code in execroot/external. [See above for how execroot is a trap. You can read more about output_base and execution_root [here](https://docs.bazel.build/versions/main/output_directories.html)]

[Linking via `bazel-<WORKSPACE_DIRECTORY_NAME>/../../external` would also have pointed to the same place, but would have broken if the workspace directory name changed. It also seemed less clean semantically.]

Another option would be having [`refresh.template.py`](./refresh.template.py) patch external paths, rather than pointing through a link. It could detect and prepend to paths starting with `"external/"` or `"bazel-out/"` with the path of the link to get equivalent behavior. However, there's some trickiness there and `//external` is a handy way for humans to browse the source code of external dependencies.

It looks like long ago—and perhaps still inside Google—Bazel created such an `//external` link. Tulsi, Bazel's XCode helper, once needed the `//external` link in the workspace to properly pick up external dependencies. This is no longer true, though you can see some of the history [here](https://github.com/bazelbuild/tulsi/issues/164).


## Choice of Strategy for Listening To Bazel

We're using bazel's [`aquery` ("action query")](https://docs.bazel.build/versions/master/aquery.html) subcommand to dump out all of the compiler invocations Bazel intends to use as part of a build. The key important features of `aquery` that make it the right choice are (1) that it operates over compile actions, so it directly gives us access to the configured compile invocations we need for `compile_commands.json` and (2) that it's comparatively really fast because it doesn't do a full build.

Previously, we'd built things using [`action_listeners`](https://docs.bazel.build/versions/master/be/extra-actions.html). (The old, original implementation lives in our git history). Action listeners are good in that, like `aquery`, they directly listen at the level of compile actions. However, they can only be invoked at the same time as a full build. This makes the first generation *really* slow—like 30m instead of aquery's 30s. Rebuilds that should hit a warm cache are also slow (~10m) if you have multiple targets, which has to be an [unnecessary cache miss bug in Bazel](https://github.com/bazelbuild/bazel/issues/13029). You might be able to get around the 30m cold issue by suppressing build with [`--nobuild`](https://docs.bazel.build/versions/master/command-line-reference.html#flag--build), though we haven't tried it and it might disrupt the output protos or otherwise not work. (Only haven't tried because we switched implementations before finding the flag.) Another key downside compared to `aquery` is that `action_listener` output accumulates in the build tree, and there's no no way to tell which outputs are fresh. There's always the risk that stale output could bork your `compile_commands.json`, so you need to widen the user interface to include a cleaning step. An additional issue is future support: action listeners are marked as deprecated in their docs, even if they are still used (at the time of writing) by Google's Kythe tooling. The main (but hopeful temporary) benefit of action listeners is that they auto-identify headers used by a given source file, which makes working around the [header commands inference issue](https://github.com/clangd/clangd/issues/123) easier. Another smaller benefit is that being integrated with building guarantees generated files have been generated. So while `action_listeners` can get the job done, they're slow and the abstraction is leaky and questionably supported.

What you really don't want to do is use `bazel query`, `bazel cquery`, or `aspect`s if you want compile commands. The primary problem with each is that they crawl the graph of bazel targets (think graph nodes), rather than actually listening to the commands Bazel is going to invoke during its compile actions. We're trying to output compile commands, not graph nodes, and it's not a 1:1 relationship! Rather than getting the commands directly with aquery, you'd have to duplicate the rule attribute -> command line logic from bazel & the toolchain, which is tough with custom toolchains, platform-specific logic, and in-graph configuration transitions for cross-compiling.   Note that `query` has issues with `select()` statements, unlike `cquery`, which takes configuration into account (hence "configured query"->cquery). Aspects are also problematic because they only propagate along attributes (like `"deps"`), breaking the chain at things like genrules, which name things differently (`"srcs"` not `"deps"` for genrules) vs. easily sorting to the right mnemonic compile actions with aquery even for actions that are for code-gen tools being built for the host. But most importantly, `query`, `cquery`, and `aspect` share the key fundamental flaw of operating on graph nodes not action-graph edges.

### Caveats

As far as we know, the only (unsolvable) problem with aquery is that it can't get the command line when sources are undetermined, like a tree artifact. Thankfully, this seems to be a very rare case edge case, handled by Bazel...but only hackily and requiring a directory with a source extension. I'm imagining that it's mostly for generated sources--which you don't much care about editing--since manually-written files can be statically listed. (Note that there isn't such a problem with headers, since we'll find them when we traverse the include graph.) See [this discussion](https://github.com/grailbio/bazel-compilation-database/pull/89#discussion_r1140927118) for an example of the problem case and some more details. If you experiment with the example therein, you'll find the aquery results for the tree artifact cc_library contain an action with the mnemonic "CppCompileActionTemplate" instead of "CppCompile" (so we'll automatically ignore these tree actions) and that there's no command line or arguments for that CppCompileActionTemplate action.

Aspects may yet be useful for generating header files without a complete build, although building the subset you care about is a common enough part of development that this hasn't been a big issue in practice. See [this issue](https://github.com/bazelbuild/bazel/issues/17660) we filed for more details on approaches, a discussion of future Bazel support, and a link to rules_xcodeproj's implementation.

## References Used When Building

There were a handful of existing implementations for getting compile commands from Bazel. None came particularly close to working well across platforms we were trying, falling into the gotchas listed in this doc, but we really appreciate the effort that went into them and the learnings we gleaned. Here they are, categorized by type and problem, sorted by closest to working.

The closest—and only aquery approach: https://github.com/thoren-d/compdb-bazel. It's Windows-clang-cl-specific, but could be a good resource when working on things for Windows. We didn't add to it because it looked stale, had a somewhat inelegant approach, and wouldn't have had much reusable code for the cross-platform focus of this tool.

Action-listener-based approaches (see pros and cons above). These helped bootstrap our initial, action-listener-based implementation.

- Medium-full implementation here, but has bugs associated with bazel structure, in addition to the execroot and wrapping issues discussed above. Perhaps bazel's directory structure has changed since it was released. https://github.com/vincent-picaud/Bazel_and_CompileCommands
  - It's based on [this gist](https://gist.github.com/bsilver8192/0115ee5d040bb601e3b7).
- Google has a heavyweight, C++ based implementation for editing their indexing tool, Kythe (originally Sythe), here, but it doesn't do any of the unwrapping we need, and we wanted to quickly iterate in python.
  - Key links:
    - https://github.com/kythe/kythe/blob/master/tools/cpp/generate_compilation_database.sh
    - https://github.com/kythe/kythe/blob/cb58e9b4b5ee911db9495b382c9fe50e936f2bb3/kythe/cxx/tools/generate_compile_commands/extract_compile_command.cc
  - Kythe also has general extraction rules for its primary job: indexing codebases
    -  https://github.com/kythe/kythe/blob/master/kythe/extractors/BUILD
    -  https://github.com/kythe/kythe/blob/master/kythe/cxx/extractor/README.md
- Another C++ impl here, but with similar issues https://github.com/tolikzinovyev/bazel-compilation-db

Aspect-based approaches. They were the most commonly used despite the approach having real problems (see above).
- https://github.com/grailbio/bazel-compilation-database is probably the most popular for generating Bazel compile_commands.json. But it's really far from working for us. No unwrapping, no ability to pick up platform flags, all the aspect issues, etc.
- Bazel's official editor plugins. Note: editor plugins, *not* compile_commands.json generators.
  - Bazel's IntelliJ adapter. Problems? All the usual subjects for aspects.
  - [Tulsi](https://github.com/bazelbuild/tulsi). Smarter about working around some of the issues, which they can do by reimplementing some of the Apple specific logic, rather than listening for it.
  - There's no support in the VSCode plugin. I'd filed https://github.com/bazelbuild/vscode-bazel/issues/179 originally.

A totally different approach that won't work with Bazel: [BEAR (Build EAR)](https://github.com/rizsotto/Bear) builds `compile_commands.json` by listening in on compiler invocations and records what it finds during a build. This lets it work across a variety of build systems...except Bazel, because it's hermeticity measures (keeping compilation as a pure function) screen out exactly the type of tinkering BEAR tries to do. It might be doable, but it would need more injection work, probably, than we'd want, and a build to listen to. See Bazel marked "wontfix" [here](https://github.com/rizsotto/Bear/issues/170).
