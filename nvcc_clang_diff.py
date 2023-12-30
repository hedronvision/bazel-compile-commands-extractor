#!/usr/bin/env python3

"""Generates a set of flags that are accepted by nvcc but not clang.

Useful for figuring out what nvcc argument patching is needed for acceptance by clangd, if we ever need to update that logic in refresh.template.py
"""

import dataclasses
import functools
import shutil
import subprocess

@functools.total_ordering
@dataclasses.dataclass
class Flag:
    long: str
    short: str
    has_args: bool

    def __lt__(self, other):
        return (self.long, self.short) < (other.long, other.short)

def flag_key(flag):
    if "=" in flag:
        return flag[:flag.index("=")]
    return flag

def get_nvcc_flags() -> list[Flag]:
    nvcc = shutil.which("nvcc") or "/usr/local/cuda/bin/nvcc"
    help_output = subprocess.check_output([nvcc, "--help"], text=True, stderr=subprocess.STDOUT)
    flags = []
    for line in help_output.splitlines():
        if not line.startswith("--"):
            continue
        # looks like --long args (-short)
        line_parts = line.split()
        short = line_parts[-1]
        if short.startswith("(") and short.endswith(")"):
            short = short[1:-1]
        flags.append(Flag(line_parts[0], short, has_args = len(line_parts) > 2))
    return flags

def get_clang_flags() -> set[str]:
    clang = shutil.which("clang") or "/usr/bin/clang"
    help_output = subprocess.check_output([clang, "--help"], text=True, stderr=subprocess.STDOUT)
    flags = set(flag_key(token) for token in help_output.split() if token.startswith("-"))
    # Fix this up manually based on https://clang.llvm.org/docs/ClangCommandLineReference.html
    flags |= {"-Wreorder", "-Wno-deprecated-declarations", "-Werror", "-O", "--help", "-l", "-m64", "--shared", "-shared"}
    return flags

def main():
    nvcc_flags = get_nvcc_flags()
    clang_flags = get_clang_flags()

    nvcc_flags_no_arg = []
    nvcc_flags_with_arg = []
    nvcc_rewrite_flags = {}
    for nvcc_flag in nvcc_flags:
        if nvcc_flag.long in clang_flags and nvcc_flag.short in clang_flags:
            continue
        if nvcc_flag.short in clang_flags:
            nvcc_rewrite_flags[nvcc_flag.long] = nvcc_flag.short
            continue
        if nvcc_flag.long in clang_flags:
            nvcc_rewrite_flags[nvcc_flag.short] = nvcc_flag.long
            continue
        if nvcc_flag.has_args:
            nvcc_flags_with_arg.append(nvcc_flag)
        else:
            nvcc_flags_no_arg.append(nvcc_flag)

    print("_nvcc_flags_no_arg = {")
    print("    # long name, short name")
    for nvcc_flag in sorted(nvcc_flags_no_arg):
        print(f"    '{nvcc_flag.long}', '{nvcc_flag.short}',")
    print("}")

    print("_nvcc_flags_with_arg = {")
    print("    # long name, short name")
    for nvcc_flag in sorted(nvcc_flags_with_arg):
        print(f"    '{nvcc_flag.long}', '{nvcc_flag.short}',")
    print("}")

    print("_nvcc_rewrite_flags = {")
    print("    # NVCC flag: clang flag")
    for nvcc_flag in sorted(nvcc_rewrite_flags):
        clang_flag = nvcc_rewrite_flags[nvcc_flag]
        print(f"    '{nvcc_flag}': '{clang_flag}',")
    print("}")

if __name__ == "__main__":
    main()
