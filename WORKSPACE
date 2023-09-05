# This file existed originally to enable quick local development via local_repository.
    # See ./ImplementationReadme.md for details on local development.
    # Why? local_repository doesn't work without a WORKSPACE, and new_local_repository requires overwriting the BUILD file (as of Bazel 7).

workspace(name = "hedron_compile_commands")

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")

# TODO(cpsauer): move everything above hedron_compile_commands_setup() into setup macros.
BAZEL_SKYLIB_VERSION = "1.4.2"

http_archive(
    name = "bazel_skylib",
    sha256 = "66ffd9315665bfaafc96b52278f57c7e2dd09f5ede279ea6d39b2be471e7e3aa",
    urls = [
        "https://mirror.bazel.build/github.com/bazelbuild/bazel-skylib/releases/download/{0}/bazel-skylib-{0}.tar.gz".format(BAZEL_SKYLIB_VERSION),
        "https://github.com/bazelbuild/bazel-skylib/releases/download/{0}/bazel-skylib-{0}.tar.gz".format(BAZEL_SKYLIB_VERSION),
    ],
)

http_archive(
    name = "rules_python",
    sha256 = "5868e73107a8e85d8f323806e60cad7283f34b32163ea6ff1020cf27abef6036",
    strip_prefix = "rules_python-0.25.0",
    url = "https://github.com/bazelbuild/rules_python/releases/download/0.25.0/rules_python-0.25.0.tar.gz",
)

load("@rules_python//python:repositories.bzl", "py_repositories")

py_repositories()

load("@rules_python//python:repositories.bzl", "python_register_toolchains")

python_register_toolchains(
    name = "python_toolchain",
    python_version = "3.11",
)

# For re-generating python_requirements_lock.bzl:
# * update python_requirements_lock.txt
# * Un-comment the below
# * run `bazel build @pip//...`,
# * cp external/pip/requirements.bzl python_requirements_lock.bzl

# load("@python_toolchain//:defs.bzl", "interpreter")
# load("@rules_python//python:pip.bzl", "pip_parse")
# pip_parse(
#     name = "pip",
#     python_interpreter_target = interpreter,
#     requirements_lock = "//:python_requirements_lock.txt",
# )

load("@hedron_compile_commands//:workspace_setup.bzl", "hedron_compile_commands_setup")

hedron_compile_commands_setup()
