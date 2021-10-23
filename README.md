# Bazel/CompileCommands — User Interface

**Goal:** *Originally, make coding more efficient and fun with cross-platform autocomplete for (Objective-)C(++) in VSCode. More generally, export (Bazel) build actions into a format that enables great tooling.*

**Status:** *Pretty great with minor rough edges. For everyday use, you probably want to use this and VSCode rather than the platform-specific adapters in AndroidStudioAdapter or XcodeAdapter, unless you need some editor feature (e.g. Apple's NextStep Interface Builder) that's not ever going to be supported in VSCode.*

## Usage

Hey Friendly Face reading this :) Let's get you some awesome tooling for the C language family.

### Start by getting `Refresh.sh` running.

It'll generate a compile_commands.json file in the root of the Bazel workspace (Product/).

That file describes how Bazel is compiling all the (Objective-)C(++) files. With the compile commands in a common format, build-system-independent tooling (e.g. clangd autocomplete, clang-tidy linting etc.), can get to work.

*Rerun `Refresh.sh` every time you want tooling (like autocomplete) to see your BUILD-file changes! Clangd will automatically pick up the changes.*

## Editor Setup -- for autocomplete based on compile_commands.json

(Add instructions for other editors as needed.)

### VSCode
Make sure you have clangd's extension installed and configured.

`code --install-extension llvm-vs-code-extensions.vscode-clangd`

In workspace settings for clangd:

- Add the following clangd flags (as written, VSCode will expand ${workspaceFolder}).
  -  They get rid of (overzealous) header insertion and are needed to  help it find the compile commands, even when browsing system headers.

In `"clangd.arguments"`
```
--header-insertion=never
--compile-commands-dir=${workspaceFolder}/Product/
```

User settings for clangd:

- Turn on: Clangd: Check Updates
  - At least until https://github.com/clangd/vscode-clangd/issues/138 is resolved. You always want the latest! New great stuff is landing and it's backwards compatible.

You may need to reload VSCode [(CMD+SHIFT+P)->reload] for the plugin to load.

If afterwards clangd doesn't prompt you to download the actual clangd server binary, hit (CMD+SHIFT+P)->check for language server updates.

## "Smooth Edges"

Does this work across target platforms? Yeah, you betcha. Expect Android completion in Android source, macOS in macOS, iOS in iOS, etc.

Does this work with generated files? Heh, hell yeah it does. But you may have to run a build for the generated file to exist.

All the usual clangd features should work. CMD+click navigation (or option if you've changed keybindings), smart rename, autocomplete, highlighting etc. Everything you expect in an IDE should be there (because most good IDEs are backed by clangd). As a general principle: If you're choosing tooling that needs to understand a programming language, you want it to be based on a compiler frontend for that language, which clangd does as part of the LLVM/clang project.


## Rough Edges

*Add here if you find more, and help delete if fixed. And let us know if you need help.*

- compile_commands.json is huge! ~350MB at the time of writing. Could we make it smaller?

  - Workaround: Sure is. It contains a description of all the ways how every file is compiled, including headers included in lots of compilations. We're hoping that, at least temporarily, you can spare the disk space.
  - Status: Need it smaller? There are things we can do but haven't because we anticipate that this problem will be temporary. The easiest thing would be to modify extract.py to only output the first entry per file. Since clangd currently chooses just one command per file anyway, this shouldn't hurt usability. We haven't done this already because clangd will hopefully [not need compile commands for headers](https://github.com/clangd/clangd/issues/519) in the future, and [will hopefully take advantage multiple compile commands per file](https://github.com/clangd/clangd/issues/681). The size without headers is only a couple MB, and without duplicate files, 16MB.

- Refresh.sh takes a while to run! ~30s at the time of writing. Could we speed it up?

  - Workaround: Probably not for now. Sorry. But if you're adding files, clangd should make pretty decent guesses at completions, using commands from nearby files; you may not need to rerun Refresh.sh on every change to BUILD files.
  - Status: The slowness is from having clang preprocess every source file in the whole project to figure out which header it uses (in extract.py). We'll need to do this until clangd does this for us in its index. [Clangd issue.](https://github.com/clangd/clangd/issues/519) Once this is fixed--and clangd 12 released with a working compile_commands.json-change-watching feature, we should consider running `Refresh.sh` automatically on build file save. Without the preprocessing, `Refresh.sh` only takes 8s or so, single threaded.

- For files compiled for multiple platforms, I'm seeing suggestions for a specific platform. Shouldn't I just see suggestions for what's accessible on all platforms?
  - Workaround: You should, but you'll have to do this in your head for now. At least there's autocomplete for everything you want to use--the issue is that there's also autocomplete for things you shouldn't use.
  - Status: Filed [an issue](https://github.com/clangd/clangd/issues/681), but the fixes are likely gnarly.

- Wish certain quick-fixes were auto-applied, maybe especially [ in Objective-C.

  - Click the quick fix for now. Sorry; I want it, too.
  - Status: Filed [an issue](https://github.com/clangd/clangd/issues/656) about Objective-C, and the more broad request.

- Some third-party no-extension header files, like `<Eigen/Dense>`, not getting syntax highlighting, autocomplete, etc. from clangd?

  - Workaround: Just mark the file as the correct language in the lower right of VSCode and things will start working.
  - Status: We'd like better auto-detect. Chris filed an [issue with VSCode-clangd](https://github.com/clangd/vscode-clangd/issues/139), (Thought that maybe it was more of a [VSCode issue](https://github.com/microsoft/vscode/issues/115826), but VSCode folks disagreed.)

- Red underlined include issues in some third-party headers?

  - Workaround: Ignore them. They're the result of people writing headers that don't include what they use and instead assume that they'll be included in a certain order. Eigen and GBDecviceInfo are examples.
  - Status: Can't think of a good way to fix--we shouldn't get into modifying 3rd party libraries to fix this. Impact is minimal. You shouldn't be editing read-only copies of 3rd party libraries anyway and it won't break autocomplete in Hedron files. The impact is that browsing the source of those libraries might be a bit more annoying.

- Could we also support Swift?

  - Workaround: Use the XcodeAdapter for now.
  - Status: There's an Apple project to (basically) add Swift support to clangd [here](https://github.com/apple/sourcekit-lsp). It doesn't look mature enough yet (2/2021), but perhaps it will be by the time we'd need it.

---
*Looking for implementation details instead?*
See [ImplementationReadme.md](./ImplementationReadme.md).
