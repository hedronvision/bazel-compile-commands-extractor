"""
Prints the arguments passed to the script
"""

import sys


def main():
  print('===HEDRON_COMPILE_COMMANDS_BEGIN_ARGS===')
  for arg in sys.argv[1:]:
    print(arg)
  print('===HEDRON_COMPILE_COMMANDS_END_ARGS===')

  # We purposely return a non-zero exit code to have the emcc process exit after running this fake clang wrapper.
  sys.exit(1)


if __name__ == '__main__':
  main()
