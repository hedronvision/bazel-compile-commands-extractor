# Hedron's Compile Commands Extractor for Bazel — User Interface

**What is this project trying to do for me?** 

*First, provide Bazel users cross-platform autocomplete for (Objective-)C(++) to make development more efficient and fun. More generally, export Bazel build actions into the compile_commands.json format that enables great tooling decoupled from Bazel.*

**Status:** *Pretty great with minor rough edges. We use this every day and love it.*

*If there haven't been commits in a while, it's because of stability, not neglect. This is in daily use at Hedron.*

*For everyday use, we'd recommend using this rather than the platform-specific IDE adapters (like Tulsi or ASwB), except the times when you need some platform-editor-specific feature (e.g. Apple's NextStep Interface Builder) that's not ever going to be supported in a cross-platform editor.*

## Usage Visuals

![Usage Animation](https://user-images.githubusercontent.com/7157583/142501309-862e89e2-02b4-4b61-950c-8b7e1bfd7eb7.gif)

▲ Extracts compile_commands.json, enabling [clangd autocomplete](https://github.com/clangd/vscode-clangd) in your editor ▼

![clangd help example](https://user-images.githubusercontent.com/7157583/142502357-af9ba056-f9e0-47ce-b69d-57e85dcca458.png)


## Usage

Howdy, Bazel user 🤠. Let's get you set up fast with some awesome tooling for the C language family.

There's a bunch of text here but only because we're trying to spell things out and make them easy. If you have issues, let us know; we'd love your help making things even better and more complete!

### First, do the usual WORKSPACE setup.

Copy this into your Bazel WORKSPACE file to add this repo as an external dependency.

```
load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")


# Hedron's Compile Commands Extractor for Bazel
# https://github.com/hedronvision/bazel-compile-commands-extractor
http_archive(
    name = "hedron_compile_commands",

    # Replace the commit hash in both places (below) with the latest. 
    # Even better, set up Renovate and let it do the work for you (see "Suggestion: Updates" below).
    url = "https://github.com/hedronvision/bazel-compile-commands-extractor/archive/9d8b3d5925728c3206010ed0062826a9faaebc2c.tar.gz",
    strip_prefix = "bazel-compile-commands-extractor-9d8b3d5925728c3206010ed0062826a9faaebc2c",
)
load("@hedron_compile_commands//:workspace_setup.bzl", "hedron_compile_commands_setup")
hedron_compile_commands_setup()
```

#### Suggestion: Updates

We'd strongly recommend you set up [Renovate](https://github.com/renovatebot/renovate) (or similar) at some point to keep this dependency (and others) up-to-date by default. [We aren't affiliated with Renovate or anything, but we think it's awesome. It watches for new versions and sends you PRs for review or automated testing. It's free and easy to set up. It's been astoundingly useful in our codebase, and and we've worked with the wonderful maintainer to polish off any rough edges for Bazel use.]

If not, maybe come back to this step later, or watch this repo for updates. [Or hey, maybe give us a quick star, while you're thinking about watching.] Like Abseil, we live at head; the latest commit to the main branch is the commit you want.

### Make external code easily browsable.

From your Bazel workspace root (i.e. `//`), run:

```ln -s bazel-out/../../../external .```

This makes it easy for you—and for build tooling—to see the external dependencies you bring in. It also makes your source tree have the same directory structure as the build sandbox. It looks like long ago—and perhaps still inside Google—Bazel automatically created such an `//external` symlink. In any event, it's a win/win to add it: It's easier for you to browse the code you use, and it eliminates whole categories of edge cases for build tooling. We'd recommend you commit this symlink to your repo so your collaborators have it, too.

### Get the extractor running.

We'll generate a compile_commands.json file in the root of the Bazel workspace (Product/).

That file describes how Bazel is compiling all the (Objective-)C(++) files. With the compile commands in a common format, build-system-independent tooling (e.g. clangd autocomplete, clang-tidy linting etc.), can get to work.

We'll get it running and then move onto the next section while it whirrs away. But in the future, every time you want tooling (like autocomplete) to see new BUILD-file changes, rerun the command you chose below! Clangd will automatically pick up the changes.

#### There are two common paths:

##### 1. Have a relatively simple codebase, where every target builds without needing any additional configuration?

In that case, just `bazel run @hedron_compile_commands//:refresh_all` 

##### 2. Often, though, you'll want to specify the output targets you care about. This avoids issues where some targets can't be built on their own; they need configuration on the command line by a parent rule. android_binaries using transitions to configure android_libraries are an example of the latter.

In that case, you can easily specify the output targets you're working on and the flags needed to build them.

Open a BUILD file—we'd recommend using (or creating) `//BUILD`—and add something like:

```
load("@hedron_compile_commands//:refresh_compile_commands.bzl", "refresh_compile_commands")

refresh_compile_commands(
    name = "refresh_compile_commands",

    # Specify the targets of interest.
    # For example, specify a dict of targets and their arguments:
    targets = {
      "//:my_output_1": "--important_flag1 --important_flag2=true, 
      "//:my_output_2": ""
    },
    # For more details, feel free to look into refresh_compile_commands.bzl if you want.
)
```


## Editor Setup — for autocomplete based on compile_commands.json

### VSCode
Make sure you have clangd's extension installed and configured.

`code --install-extension llvm-vs-code-extensions.vscode-clangd`

Open VSCode workspace settings.

Add the following clangd flags (as written, VSCode will expand ${workspaceFolder}).
  -  They get rid of (overzealous) header insertion and are needed to  help it find the compile commands, even when browsing system headers.
  -  If your Bazel WORKSPACE is a subdirectory of you project, change --compile-commands-dir to point into that subdirectory

In `"clangd.arguments"`
```
--header-insertion=never
--compile-commands-dir=${workspaceFolder}/
```

In VSCode user settings:

Turn on: Clangd: Check Updates
  - At least until https://github.com/clangd/vscode-clangd/issues/138 is resolved. You always want the latest! New great stuff is landing in clangd and it's backwards compatible.

You may need to reload VSCode [(CMD/CTRL+SHIFT+P)->reload] for the plugin to load.

If afterwards clangd doesn't prompt you to download the actual clangd server binary, hit (CMD/CTRL+SHIFT+P)->check for language server updates.

### Other Editors

If you're using another editor, you'll need to follow the same rough steps as above: get clangd set up to extend the editor and then supply the flags.

Once you've succeeded in setting up another editor—or set up clangtidy, or otherwise seen a way to improve this tool—we'd love it if you'd contribute what you know!

## "Smooth Edges" — what we've enjoyed using this for.

You should now be all set to go! Here's what you should be expecting, based on our experience:

We use this tool every day to develop a cross-platform library for iOS and Android on macOS. Expect Android completion in Android source, macOS in macOS, iOS in iOS, etc. 

All the usual clangd features should work. CMD/CTRL+click navigation (or option if you've changed keybindings), smart rename, autocomplete, highlighting etc. Everything you expect in an IDE should be there (because most good IDEs are backed by clangd). As a general principle: If you're choosing tooling that needs to understand a programming language, you want it to be based on a compiler frontend for that language, which clangd does as part of the LLVM/clang project.

Everything should also work for generated files, though you may have to run a build for the generated file to exist.

We think it'll work for Android and Linux on Linux (but aren't using it for that yet; let us know your experience in an issue!). We'd expect Windows to need some patching parallel to that for macOS (in [extract.py](./extract.py)), but it should be a relatively easy adaptation compared to writing things from scratch. If you fall into either case, let us know. We'd love to work together to get things working smoothly on other host platforms.

## Rough Edges

We've self-filed issues for the rough edges we know about and are tracking. We'd love to hear from you there about what you're seeing, good and bad. Please add things if you find more rough edges, and let us know if you need help.

We'd also love to work with you on contributions, of course! Development setup isn't onerous; we've got [a great doc to guide you quickly into being able to make the changes you need.](./ImplementationReadme.md)

---
*Looking for implementation details instead? Want to dive into the codebase?*
See [ImplementationReadme.md](./ImplementationReadme.md).

*Bazel/Blaze maintainer reading this?* If you'd be interested in integrating this into official Bazel tools, let us know in an issue or email, and let's talk! We love getting to use Bazel and would love to help.
