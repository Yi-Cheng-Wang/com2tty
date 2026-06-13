"""Spawning and supervising the helper process that runs inside WSL.

The entire bridge transport is the stdin/stdout/stderr of one ``wsl --exec``
child process; this module owns how that child is built (argument quoting,
path translation, environment verification) and how it is torn down.
"""
import logging
import os
import shutil
import subprocess

from ..core.constants import CREATE_NO_WINDOW


def wsl_command(distro, *argv):
    """Build a wsl.exe invocation that bypasses the WSL login shell.

    Without ``--exec``, wsl.exe re-joins its arguments and hands them to the
    distro's default shell, which re-splits on whitespace. Any path containing
    a space (e.g. /mnt/c/Program Files/...) would break. ``--exec`` launches
    the binary directly with the arguments preserved as-is.
    """
    cmd = ["wsl"]
    if distro:
        cmd += ["-d", distro]
    cmd.append("--exec")
    cmd.extend(argv)
    return cmd


def get_wsl_path(win_path, distro=None):
    cmd = wsl_command(distro, "wslpath", "-u", win_path)
    try:
        # wslpath emits UTF-8 regardless of the Windows locale; decoding with
        # the ANSI codepage would corrupt non-ASCII paths (e.g. CJK usernames).
        res = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", check=True)
        return res.stdout.strip()
    except Exception as e:
        logging.debug(f"wslpath failed: {e}. Using fallback conversion.")
        # Fallback to mounting convention /mnt/<drive>/...
        drive = win_path[0].lower()
        path = win_path[2:].replace("\\", "/")
        return f"/mnt/{drive}{path}"


def check_wsl_environment(wsl_script_path=None, distro=None):
    """Verify WSL prerequisites before spawning the bridge.

    Each failure mode otherwise surfaces as a cryptic error (WinError 2, an
    instantly-exiting subprocess, a 'No such file' from deep inside WSL), so
    fail fast here with an actionable message instead.
    """
    target = f"WSL distribution '{distro}'" if distro else "the default WSL distribution"

    if shutil.which("wsl") is None:
        raise RuntimeError(
            "wsl.exe was not found in PATH. com2tty requires Windows Subsystem "
            "for Linux. Install it with 'wsl --install' and try again."
        )

    try:
        res = subprocess.run(
            wsl_command(distro, "python3", "--version"),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to start {target}: {e}")

    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip()
        raise RuntimeError(
            f"'python3' is not available in {target} ({detail or 'no output'}). "
            "Check 'wsl -l -v' for installed distributions, select one with "
            "--distro, or install Python inside WSL (e.g. 'sudo apt install python3')."
        )

    if wsl_script_path:
        res = subprocess.run(
            wsl_command(distro, "test", "-r", wsl_script_path),
            capture_output=True, timeout=30,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"The com2tty bridge script is not readable from WSL at "
                f"{wsl_script_path}. Make sure Windows drive automounting is "
                "enabled in WSL ([automount] in /etc/wsl.conf must not be "
                "disabled) and that the install path is accessible from WSL."
            )


def helper_script_path(name):
    """Windows path of a WSL helper entry script.

    The entry scripts (bridge.py, pad_bridge.py) live at the com2tty package
    root -- one directory above this ``windows`` subpackage -- so that the
    path handed to ``wsl --exec python3 -u <path>`` stays stable.
    """
    package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(package_dir, name)


def spawn_wsl_helper(cmd):
    """Start a WSL helper with raw, unbuffered stdio pipes.

    CREATE_NO_WINDOW prevents wsl.exe from modifying the Windows console
    mode, which would otherwise disable Ctrl+C (ENABLE_PROCESSED_INPUT) for
    the Python CLI.
    """
    logging.info(f"Spawning WSL process: {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        creationflags=CREATE_NO_WINDOW,
    )


def terminate_wsl_helper(proc, timeout=3.0):
    """Terminate the helper, escalating to kill if it does not exit."""
    if proc.poll() is None:
        logging.info("Terminating WSL process...")
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logging.warning("WSL process did not exit. Killing it.")
            proc.kill()
