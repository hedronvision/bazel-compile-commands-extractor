import unittest
import types

from command_reformatter import CommandReformatter

windows_default_include_paths = ()

def _print_header_finding_warning_once():
    pass

def _get_bazel_cached_action_keys():
    pass

def _get_cached_adjusted_modified_time():
    pass

def _get_apple_SDKROOT(_: str):
    return '/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk'

def _get_apple_DEVELOPER_DIR():
    return '/Library/Developer/CommandLineTools'

def _get_apple_active_clang():
    return '/Library/Developer/CommandLineTools/usr/bin/clang'

class TestCommandReformatter(unittest.TestCase):
    def setUp(self):
        self.reformatter = CommandReformatter(False,
                                              windows_default_include_paths,
                                              _print_header_finding_warning_once,
                                              _get_bazel_cached_action_keys,
                                              _get_cached_adjusted_modified_time,
                                              _get_apple_SDKROOT,
                                              _get_apple_DEVELOPER_DIR,
                                              _get_apple_active_clang)

    def test_basic(self):
        self.assertEqual(
            self.reformatter.reformat(types.SimpleNamespace(arguments = ["gcc", "foo.c", "-o", "foo.o"])),
            ({'foo.c'}, set(), ['gcc', 'foo.c', '-o', 'foo.o']))

    def test_basic_apple(self):
        self.assertEqual(
            self.reformatter.reformat(types.SimpleNamespace(arguments = [
                "external/local_config_cc/wrapped_clang_pp",
                "DEBUG_PREFIX_MAP_PWD\u003d.",
                "-isysroot",
                "__BAZEL_XCODE_SDKROOT__",
                "-F__BAZEL_XCODE_SDKROOT__/System/Library/Frameworks",
                "-F__BAZEL_XCODE_DEVELOPER_DIR__/Platforms/MacOSX.platform/Developer/Library/Frameworks",
                "-c",
                "foo.c",
                "-o",
                "bazel-out/foo.o"
            ])),
            (
                {'foo.c'},
                set(),
                [
                    _get_apple_active_clang(),
                    "-isysroot",
                    _get_apple_SDKROOT(''),
                    f"-F{_get_apple_SDKROOT('')}/System/Library/Frameworks",
                    f"-F{_get_apple_DEVELOPER_DIR()}/Platforms/MacOSX.platform/Developer/Library/Frameworks",
                    "-c",
                    "foo.c",
                    "-o",
                    "bazel-out/foo.o"
                ]
            )
        )

unittest.main()
