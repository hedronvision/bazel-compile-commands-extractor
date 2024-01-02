// Prints the arguments passed to the script

#include <iostream>

int main(int argc, char *argv[]) {
  std::cout << "===HEDRON_COMPILE_COMMANDS_BEGIN_ARGS===\n";
  for (int i = 1; i < argc; ++i) {
    std::cout << argv[i] << "\n";
  }
  std::cout << "===HEDRON_COMPILE_COMMANDS_END_ARGS===\n";
  // We purposely return a non-zero exit code to have the emcc process exit after running this fake clang wrapper.
  return 1;
}
