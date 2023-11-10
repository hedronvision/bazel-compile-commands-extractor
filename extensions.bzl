"""bzlmod extensions file to run the hedron_compile_commands_setup function
This will run the hedron_compile_commands_setup function from workspace_setup.bzl,
(even though it's currently empty), using the extensions system from the new bzlmod system
"""
load("@hedron_compile_commands//:workspace_setup.bzl", "hedron_compile_commands_setup")

def _hedron_setup_impl(ctx):
    hedron_compile_commands_setup()

hedron_compile_commands_extension = module_extension(
    implementation = _hedron_setup_impl,
)
