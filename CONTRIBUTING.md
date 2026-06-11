# Contributing to com2tty

This document describes how to contribute to com2tty. It complements the
[README](README.md), which covers installation and usage, and the
[architecture document](ARCHITECTURE.md), which explains the internal design.

## Reporting bugs

File bugs as issues on the project's GitHub repository. A useful report states
the com2tty version, obtained with `com2tty --version`; the Windows version and
the WSL distribution, obtained with `wsl -l -v`; and the Python version on both
the Windows host and inside WSL. Describe the exact command that was run, the
board or controller involved, and the observed behaviour against the expected
behaviour. Attach the output of the same command run with the `-d` or `--debug`
flag, which enables verbose logging on standard error, including the control
protocol exchanged with the WSL helper. When the problem concerns board
detection or upload, include the output of `com2tty --list` so that the device's
USB identity and the detected board family are visible.

## Proposing features

Open an issue that describes the use case before writing code for a substantial
feature. State the hardware involved, the workflow the feature would enable, and
why the existing modes do not cover it. Because the WSL helper is constrained to
the Python standard library and the host depends only on `pyserial`, a proposal
that would add a runtime dependency should explain why the standard library is
insufficient.

## Branching and pull requests

Base every feature branch on the `develop` branch and open pull requests against
`develop`, not against `main`. The `main` branch is reserved for releases; a push
to `main` triggers the publishing job described below. Name branches after the
change they carry, for example `feature/device-discovery` or `fix/uf2-reopen`.

A pull request is expected to keep the continuous integration pipeline green.
That pipeline runs the ruff linter and the full test suite on both
`windows-latest` and `ubuntu-latest` against every supported Python version, and
it fails the build if line coverage is below one hundred percent. Add or update
tests for any behavioural change, and update the README, the changelog, and the
architecture document when the change affects user-facing behaviour or the
internal design.

## Commit message convention

Commit messages follow the Conventional Commits format,
`<type>(<scope>): <description>`, as established throughout the project history.
The type is one of `feat`, `fix`, `refactor`, `perf`, `test`, `docs`, `style`,
`chore`, `ci`, `build`, or `revert`. The scope names the affected area, for
example `host`, `gamepad`, `bridge`, or `readme`. The description is written in
the imperative mood and does not end with a period. Representative examples from
the history are `feat(gamepad): forward XInput controllers into WSL`,
`fix(host): prevent PowerShell injection and self-heal crash residue`, and
`test(host): cover security fixes and branch paths to 100%`.

## Code style and linting

The project is linted with ruff, configured in `pyproject.toml`. The configured
rule set is correctness-focused: it enables the pyflakes checks and the
error-class pycodestyle checks rather than enforcing a comprehensive style guide.
Run the linter before opening a pull request.

```bash
ruff check src tests scripts
```

Match the surrounding code rather than imposing a personal style. New code should
read like the module it lives in, with the same naming conventions, comment
density, and idioms. The WSL-side modules, `bridge.py` and `pad_bridge.py`, must
import successfully on Windows for the test suite to run there, which is why
`tests/conftest.py` substitutes a mock `termios` module; keep new WSL-side code
importable under that constraint by deferring Linux-only imports or guarding them.

## Running the tests

Install the package in editable mode together with the test and lint tools.

```bash
pip install -e .
pip install pytest pytest-cov ruff
```

Run the suite with coverage.

```bash
pytest --cov=src/com2tty --cov-report=term-missing tests/
```

A passing run reports every test passing and full line coverage for the package.
The suite is cross-platform: it mocks the platform-specific system calls used by
the gamepad sinks and the WSL-only `termios` module so that it runs on both
Windows and Linux without real hardware or a real WSL kernel.

## Manual end-to-end verification

Continuous integration has no WSL instance and no serial or controller hardware,
so the automated suite mocks every system boundary. Before a release, exercise
the real path with the manual smoke test under `scripts/`. Run it on a Windows
host with a device attached and WSL installed.

```cmd
python scripts/e2e_smoke.py --port COM17
```

The script checks that `--list` reports the port, starts a bridge, verifies that
the serial symlink and the RFC 2217 forwarder appear inside WSL, and, when the
device has its transmit line wired to its receive line, verifies an echo
round-trip with the `--loopback` flag. Hardware-specific behaviour that the
automated suite cannot reach, listed in the known-limitations section of the
architecture document, should be confirmed on real hardware before being relied
upon.

## Legal

Contributions are accepted under the project's MIT License. By submitting a
contribution you agree that it may be distributed under that licence. The project
does not currently require a contributor licence agreement or a developer
certificate of origin sign-off.
