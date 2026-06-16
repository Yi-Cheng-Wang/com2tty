"""Environment self-diagnosis for ``com2tty --doctor``.

Nearly every com2tty failure mode is environmental: a missing WSL, a default
distribution without python3, a Windows build without ``wsl --exec``, a TCP
port already in use, or leftovers from a crashed session. This module runs
the same prerequisite probes the bridge performs at startup -- plus a few
that it cannot afford at startup -- and prints one actionable line per check.

Exit status is 0 when nothing failed (warnings are informational) and 1 when
at least one required check failed.
"""
import os
import shutil
import subprocess

from .wsl_process import (
    CONSOLE_NEUTRAL,
    get_wsl_path,
    helper_script_path,
    wsl_command,
)
from .os_hacks.autoplay import _autoplay_marker_path

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"


def _run(cmd, timeout=30):
    """Run a command; return (returncode or None on error, stdout, stderr)."""
    try:
        # Console-neutral: a doctor probe may be the first command to cold-boot
        # the WSL VM, and must not bind its terminal to the caller's console
        # (see CONSOLE_NEUTRAL in wsl_process).
        res = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             timeout=timeout, **CONSOLE_NEUTRAL)
        return res.returncode, (res.stdout or "").strip(), (res.stderr or "").strip()
    except Exception as e:
        return None, "", str(e)


def check_wsl_exe():
    path = shutil.which("wsl")
    if path is None:
        return (FAIL, "wsl.exe on PATH",
                "not found; install WSL with 'wsl --install' from an "
                "elevated prompt")
    return (OK, "wsl.exe on PATH", path)


def check_wsl_exec(distro):
    """`wsl --exec` exists since Windows 10 1903; com2tty depends on it."""
    rc, out, err = _run(wsl_command(distro, "true"))
    if rc == 0:
        return (OK, "wsl --exec support", "")
    return (FAIL, "wsl --exec support",
            (err or out or "no output") +
            " (Windows 10 1903 or later is required)")


def check_python3(distro):
    rc, out, err = _run(wsl_command(distro, "python3", "--version"))
    if rc == 0:
        return (OK, "python3 in WSL", out or err)
    return (FAIL, "python3 in WSL",
            (err or out or "no output") + " -- select another distribution "
            "with --distro, or 'sudo apt install python3' inside WSL")


def check_bridge_script(distro):
    script = helper_script_path("bridge.py")
    wsl_path = get_wsl_path(script, distro)
    rc, out, err = _run(wsl_command(distro, "test", "-r", wsl_path))
    if rc == 0:
        return (OK, "bridge script readable from WSL", wsl_path)
    return (FAIL, "bridge script readable from WSL",
            f"{wsl_path} is not readable; Windows drive automounting may be "
            "disabled ([automount] in /etc/wsl.conf)")


def check_fuser(distro):
    rc, out, err = _run(wsl_command(distro, "which", "fuser"))
    if rc == 0:
        return (OK, "fuser available in WSL", out)
    return (WARN, "fuser available in WSL",
            "not found; leftover-listener cleanup is disabled "
            "('sudo apt install psmisc')")


def check_ports(distro, rfc2217_port, fuser_ok):
    """Probe the RFC 2217 and UF2 relay TCP ports inside WSL."""
    results = []
    for port, label in ((rfc2217_port, "RFC 2217 port"),
                        (rfc2217_port + 1, "UF2 relay port")):
        name = f"{label} {port} free in WSL"
        if not fuser_ok:
            results.append((SKIP, name, "fuser is unavailable"))
            continue
        rc, out, err = _run(wsl_command(distro, "fuser", f"{port}/tcp"))
        if rc is None:
            results.append((SKIP, name, err))
        elif rc == 0:
            results.append((WARN, name,
                            f"in use by PID(s) {out or err}; pick another "
                            "--rfc2217-port"))
        else:
            results.append((OK, name, ""))
    return results


# Probe run inside WSL with python3 (already verified): counts intercepted
# picotool binaries and rc files still carrying an injection block.
_LEFTOVER_PROBE = (
    "import glob,os;"
    "home=os.path.expanduser('~');"
    "real=glob.glob(os.path.join(home,'.platformio','packages',"
    "'tool-picotool*','picotool.real'));"
    "rc=[p for p in (os.path.join(home,'.bashrc'),os.path.join(home,'.zshrc'))"
    " if os.path.exists(p) and 'COM2TTY INJECTION' in"
    " open(p,errors='replace').read()];"
    "print(len(real),len(rc))"
)


def check_leftovers(distro):
    rc, out, err = _run(wsl_command(distro, "python3", "-c", _LEFTOVER_PROBE))
    if rc != 0:
        return (SKIP, "leftovers from crashed sessions", err or out)
    try:
        n_real, n_rc = (int(token) for token in out.split())
    except ValueError:
        return (SKIP, "leftovers from crashed sessions",
                f"unexpected probe output: {out!r}")
    if n_real == 0 and n_rc == 0:
        return (OK, "leftovers from crashed sessions", "none")
    details = []
    if n_real:
        details.append(f"{n_real} intercepted picotool binary(ies)")
    if n_rc:
        details.append(f"{n_rc} rc file(s) with an injected block")
    return (WARN, "leftovers from crashed sessions",
            ", ".join(details) + " -- stale ones are reclaimed automatically "
            "on the next bridge start")


def check_uinput(distro):
    rc, out, err = _run(wsl_command(distro, "test", "-w", "/dev/uinput"))
    if rc == 0:
        return (OK, "/dev/uinput writable (gamepad --uinput)", "")
    return (WARN, "/dev/uinput writable (gamepad --uinput)",
            "not accessible; only needed for --gamepad --uinput "
            "(see the README for the one-time setup)")


# Probe run inside WSL with python3: for each /tmp endpoint passed as an
# argument, find a /dev/* symlink that resolves to the same live device, so the
# dashboard can auto-discover a `sudo ln -sf <tmp> /dev/<name>` alias the user
# created -- whatever name they chose -- without being told it in advance. The
# endpoint's real target must be a live pty slave (/dev/pts/N) for a match to
# count, so a stale/dangling link never reports a phantom alias.
_DEV_ALIAS_PROBE = (
    "import os,sys\n"
    "def find(tmp):\n"
    " try: target=os.path.realpath(tmp)\n"
    " except OSError: return ''\n"
    " if not (target.startswith('/dev/pts/') and os.path.exists(target)): return ''\n"
    " try: names=sorted(os.listdir('/dev'))\n"
    " except OSError: return ''\n"
    " for name in names:\n"
    "  p='/dev/'+name\n"
    "  if os.path.islink(p):\n"
    "   try:\n"
    "    if os.path.realpath(p)==target: return p\n"
    "   except OSError: pass\n"
    " return ''\n"
    # Emit '-' (not an empty field) when no alias is found, so the marker
    # survives the caller's .strip() of the captured stdout's trailing line.
    "for tmp in sys.argv[1:]:\n"
    " sys.stdout.write(tmp+'\\t'+(find(tmp) or '-')+'\\n')\n"
)


def find_dev_aliases(distro, tmp_paths):
    """Map each ``/tmp`` endpoint to a ``/dev/*`` symlink aliasing it, or None.

    Runs one probe inside WSL that scans ``/dev`` for a top-level symlink whose
    real target matches each endpoint's live pseudo-terminal slave. This is how
    the dashboard auto-detects a ``sudo ln -sf /tmp/ttyUSB0 /dev/ttyACM0`` alias
    the user created by hand, with any name, and reflects it in the Endpoint
    column -- no ``/dev`` name has to be configured in advance. Returns ``{}``
    on any failure so the caller simply shows the ``/tmp`` paths.
    """
    paths = [p for p in (tmp_paths or []) if p]
    if not paths:
        return {}
    rc, out, _err = _run(wsl_command(distro, "python3", "-c",
                                     _DEV_ALIAS_PROBE, *paths))
    aliases = {}
    if rc != 0:
        return aliases
    for line in out.splitlines():
        if "\t" in line:
            tmp, dev = line.split("\t", 1)
            aliases[tmp] = None if dev in ("", "-") else dev
    return aliases


def check_autoplay_marker():
    if os.path.exists(_autoplay_marker_path()):
        return (WARN, "AutoPlay recovery marker",
                "present; a previous session crashed mid-flash "
                "(restored automatically on the next run)")
    return (OK, "AutoPlay recovery marker", "none")


def check_xinput(os_name=os.name):
    if os_name != "nt":
        return (SKIP, "XInput DLL (gamepad)", "not a Windows host")
    try:
        from .gamepad_host import _load_xinput
        _load_xinput()
        return (OK, "XInput DLL (gamepad)", "")
    except Exception as e:
        return (WARN, "XInput DLL (gamepad)", str(e))


def collect_doctor_results(distro=None, rfc2217_port=4000):
    """Run every applicable check and return a list of (status, label, detail).

    The check chain short-circuits the WSL-dependent probes when WSL itself
    is missing, exactly like ``run_doctor`` does when printing. Split out so
    callers that want structured results (e.g. the dashboard's Doctor tab)
    can render them however they like instead of scraping printed text.
    """
    results = [check_wsl_exe()]
    wsl_ok = results[0][0] == OK
    if wsl_ok:
        results.append(check_wsl_exec(distro))
        wsl_ok = results[-1][0] == OK
    if wsl_ok:
        python_check = check_python3(distro)
        results.append(python_check)
        results.append(check_bridge_script(distro))
        fuser_check = check_fuser(distro)
        results.append(fuser_check)
        results.extend(check_ports(distro, rfc2217_port,
                                   fuser_check[0] == OK))
        if python_check[0] == OK:
            results.append(check_leftovers(distro))
        results.append(check_uinput(distro))
    results.append(check_autoplay_marker())
    results.append(check_xinput())
    return results


def run_doctor(distro=None, rfc2217_port=4000):
    """Run all checks, print one line per result, return the exit status."""
    # Each WSL probe can block for up to 30s if WSL is cold-starting or hung;
    # without this the tool looks frozen while it waits.
    print("Running com2tty environment checks (WSL probes can take a few "
          "seconds each if WSL is starting up)...", flush=True)
    results = collect_doctor_results(distro, rfc2217_port)

    for status, label, detail in results:
        line = f"[{status:>4}] {label}"
        if detail:
            line += f" -- {detail}"
        print(line)

    failed = sum(1 for status, _, _ in results if status == FAIL)
    warned = sum(1 for status, _, _ in results if status == WARN)
    print()
    if failed:
        print(f"{failed} check(s) failed, {warned} warning(s); com2tty will "
              "not work until the failures above are fixed.")
        return 1
    if warned:
        print(f"All required checks passed ({warned} warning(s)).")
        return 0
    print("All checks passed.")
    return 0
