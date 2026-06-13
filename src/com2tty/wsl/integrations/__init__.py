"""Developer-tooling integrations the bridge maintains inside WSL.

These make PlatformIO and picotool work transparently against the bridged
device: environment variables injected into the user's shell configuration,
and an interception of the picotool binary that reroutes UF2 uploads
through the bridge's relay. Both are self-healing -- a crashed session's
leftovers are detected and reclaimed on the next start, while artifacts
owned by a *live* concurrent session are left untouched.
"""
