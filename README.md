# Hedron's Compile Commands Extractor for Bazel — User Interface

**What is this project trying to do for me?** 

First, provide Bazel users cross-platform autocomplete for the C language family (C++, C, Objective-C, and Objective-C++), and thereby make development more efficient and fun!

More generally, export Bazel build actions into the compile_commands.json format that enables great tooling decoupled from Bazel.

## Usage Visuals

![Usage Animation](https://user-images.githubusercontent.com/7157583/142501309-862e89e2-02b4-4b61-950c-8b7e1bfd7eb7.gif)

▲ Extracts compile_commands.json, enabling [clangd autocomplete](https://github.com/clangd/vscode-clangd) in your editor ▼

![clangd help example](https://user-images.githubusercontent.com/7157583/142502357-af9ba056-f9e0-47ce-b69d-57e85dcca458.png)

## Status

Pretty great with only very minor rough edges. We use this every day and love it.

If there haven't been commits in a while, it's because of stability, not neglect. This is in daily use inside Hedron.

For everyday use, we'd recommend using this rather than the platform-specific IDE adapters (like Tulsi or ASwB), except the times when you need some platform-editor-specific feature (e.g. Apple's NextStep Interface Builder) that's not ever going to be supported in a cross-platform editor.

### Outside Testimonials

There are lots of people using this tool. That includes large companies and projects with tricky stacks, like in robotics.

We're including a couple of things they've said. We hope they'll give you enough confidence to give this tool a try, too!

> "Thanks for an awesome tool! Super easy to set up and use."
— a robotics engineer at Boston Dynamics

> "Thank you for showing so much rigor in what would otherwise be just some uninteresting tooling project. This definitely feels like a passing the baton/torch moment. My best wishes for everything you do in life."
— author of the previous best tool of this type

## Usage

Howdy, Bazel user 🤠. Let's get you set up fast with some awesome tooling for the C language family.

There's a bunch of text here but only because we're trying to spell things out and make them easy. If you have issues, let us know; we'd love your help making things even better and more complete—and we'd love to help you!

### First, do the usual WORKSPACE setup.

Copy this into your Bazel WORKSPACE file to add this repo as an external dependency, making sure to update to the [latest commit](https://github.com/hedronvision/bazel-compile-commands-extractor/commits/main) per the instructions below.

```Starlark
load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")


# Hedron's Compile Commands Extractor for Bazel
# https://github.com/hedronvision/bazel-compile-commands-extractor
http_archive(
    name = "hedron_compile_commands",

    # Replace the commit hash in both places (below) with the latest, rather than using the stale one here. 
    # Even better, set up Renovate and let it do the work for you (see "Suggestion: Updates" in the README).
    url = "https://github.com/hedronvision/bazel-compile-commands-extractor/archive/140666077ab4ca7f10041080e8b55cf641c07d30.tar.gz",
    strip_prefix = "bazel-compile-commands-extractor-140666077ab4ca7f10041080e8b55cf641c07d30",
    # When you first run this tool, it'll recommend a sha256 hash to put here with a message like: "DEBUG: Rule 'hedron_compile_commands' indicated that a canonical reproducible form can be obtained by modifying arguments sha256 = ..." 
)
load("@hedron_compile_commands//:workspace_setup.bzl", "hedron_compile_commands_setup")
hedron_compile_commands_setup()
```

#### Suggestion: Updates

We'd strongly recommend you set up [Renovate](https://github.com/renovatebot/renovate) (or similar) to keep this dependency (and others) up-to-date by default. [We aren't affiliated with Renovate or anything, but we think it's awesome. It watches for new versions and sends you PRs for review or automated testing. It's free and easy to set up. It's been astoundingly useful in our codebase, and we've worked with the wonderful maintainer to make things great for Bazel use.]

If not now, maybe come back to this step later, or watch this repo for updates. [Or hey, maybe give us a quick star, while you're thinking about watching.] Like Abseil, we live at head; the latest commit to the main branch is the commit you want.

### Make external code easily browsable. (also necessary for this tool to function correctly)

From your Bazel workspace root (i.e. `//`), run:

```ln -s bazel-out/../../../external .```

This makes it easy for you—and for build tooling—to see the external dependencies you bring in. It also makes your source tree have the same directory structure as the build sandbox. It looks like long ago—and perhaps still inside Google—Bazel automatically created such an `//external` symlink. In any event, it's a win/win to add it: It's easier for you to browse the code you use, and it eliminates whole categories of edge cases for build tooling. We'd recommend you commit this symlink to your repo so your collaborators have it, too, because they'll need it for this tool to work.

### Get the extractor running.

We'll generate a compile_commands.json file in the root of the Bazel workspace (Product/).

That file describes how Bazel is compiling all the (Objective-)C(++) files. With the compile commands in a common format, build-system-independent tooling (e.g. clangd autocomplete, clang-tidy linting etc.), can get to work.

We'll get it running and then move onto the next section while it whirrs away. But in the future, every time you want tooling (like autocomplete) to see new BUILD-file changes, rerun the command you chose below! Clangd will automatically pick up the changes.

#### There are two common paths:

##### 1. Have a relatively simple codebase, where every target builds without needing any additional configuration?

In that case, just `bazel run @hedron_compile_commands//:refresh_all` 

##### 2. Often, though, you'll want to specify the output targets you care about. This avoids issues where some targets can't be built on their own; they need configuration on the command line or by a parent rule. An example of the latter is an android_library, which probably cannot be built independently of the android_binary that configures it.

In that case, you can easily specify the top-level output targets you're working on and the flags needed to build them.

Open a BUILD file—we'd recommend using (or creating) `//BUILD`—and add something like:

```Starlark
load("@hedron_compile_commands//:refresh_compile_commands.bzl", "refresh_compile_commands")

refresh_compile_commands(
    name = "refresh_compile_commands",

    # Specify the targets of interest.
    # For example, specify a dict of targets and any flags required to build.
    # (No need to add flags already in .bazelrc. They're automatically picked up.)
    targets = {
      "//:my_output_1": "--important_flag1 --important_flag2=true", 
      "//:my_output_2": "",
    },
    # If you don't need flags, a list of targets is also okay, as is a single target string.
    # For more details, feel free to look into refresh_compile_commands.bzl if you want.
)
```


## Editor Setup — for autocomplete based on compile_commands.json

### VSCode
Let's get clangd's extension installed and configured.

```
code --install-extension llvm-vs-code-extensions.vscode-clangd
# We also need make sure that Microsoft's C++ extension is not involved and interfering.
code --uninstall-extension ms-vscode.cpptools
```

Then, open VSCode user settings.

Add the following two, separate entries to `"clangd.arguments"`:
```
--header-insertion=never
--compile-commands-dir=${workspaceFolder}/
```
(Just copy each as written; VSCode will correctly expand ${workspaceFolder} for each workspace.)
  -  They get rid of (overzealous) header insertion and are needed to  help it find the compile commands, even when browsing system headers outside the source tree.
  -  If your Bazel WORKSPACE is a subdirectory of your project, change --compile-commands-dir to point into that subdirectory by overriding *both* flags in your *workspace* settings


Turn on: Clangd: Check Updates
  - At least until https://github.com/clangd/vscode-clangd/issues/138 is resolved. You always want the latest! New great stuff is landing in clangd and it's backwards compatible.

If afterwards clangd doesn't prompt you to download the actual clangd server binary, hit (CMD/CTRL+SHIFT+P)->Download language Server.

You may need to subsequently reload VSCode [(CMD/CTRL+SHIFT+P)->reload] for the plugin to load. The clangd download should prompt you to do so when it completes.

### Other Editors

If you're using another editor, you'll need to follow the same rough steps as above: [get clangd set up to extend the editor](https://clangd.llvm.org/installation.html#editor-plugins) and then supply the flags.

Once you've succeeded in setting up another editor—or set up clangtidy, or otherwise seen a way to improve this tool—we'd love it if you'd contribute what you know!

## "Smooth Edges" — what we've enjoyed using this for.

You should now be all set to go! Way to make it through setup.

There should be a compile_commands.json file in the root of your workspace, enabling your editor to provide great, clang-based autocomplete. And you should know what target to `bazel run` to refresh that autocomplete, when you make BUILD-file changes big enough to require a refresh.

Behind the scenes, that compile_commands.json file contains entries describing all the commands used to build every source file in your project. And, for now, there's also one entry per header, describing one way it is compiled. (This gets you great autocomplete in header files, too, so you don't have to think about [clangd's biggest rough edge](https://github.com/clangd/clangd/issues/123)). Crucially, all these commands have been sufficiently de-Bazeled for clang tooling (or you!) to understand them.


### Here's what you should be expecting, based on our experience:

We use this tool every day to develop a cross-platform library for iOS and Android on macOS. Expect Android completion in Android source, macOS in macOS, iOS in iOS, etc. We have people using it on Linux/Ubuntu, too.

All the usual clangd features should work. CMD/CTRL+click navigation (or option if you've changed keybindings), smart rename, autocomplete, highlighting etc. Everything you expect in an IDE should be there (because most good IDEs are backed by clangd). As a general principle: If you're choosing tooling that needs to understand a programming language, you want it to be based on a compiler frontend for that language, which clangd does as part of the LLVM/clang project.

Everything should also work for generated files, though you may have to run a build for the generated file to exist.

We haven't yet tested on Windows. Windows might need some patching parallel to that for macOS (in [refresh.template.py](./refresh.template.py)), but it should be a relatively easy adaptation compared to writing things from scratch. If you're trying to use it on Windows, let us know [here](https://github.com/hedronvision/bazel-compile-commands-extractor/issues/8). We'd love to work together to get things working smoothly.

## Rough Edges

We've self-filed issues for the rough edges we know about and are tracking. We'd love to hear from you there about what you're seeing, good and bad. Please add things if you find more rough edges, and let us know if you need help or more features.

On the other hand, if you've set things up and they're working well, we'd still love to hear from you. Please file a "non-issue" in the issues tab describing your success! We'd love to hear what you're working on, what platforms you're using, and what you're finding most useful. And maybe also toss a star our way so we know it was helpful to you.

We'd also love to work with you on contributions and improvements, of course! Development setup isn't onerous; we've got [a great doc to guide you quickly into being able to make the changes you need.](./ImplementationReadme.md) The codebase is super clean and friendly. Stepping into the code is a fun and efficient way to get the improvements you want.

---
*Looking for implementation details instead? Want to dive into the codebase?*
See [ImplementationReadme.md](./ImplementationReadme.md).

*Bazel/Blaze maintainer reading this?* If you'd be interested in integrating this into official Bazel tools, let us know in an issue or email, and let's talk! We love getting to use Bazel and would love to help.
