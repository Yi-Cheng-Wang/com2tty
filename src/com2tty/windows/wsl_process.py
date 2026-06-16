"""Spawning and supervising the helper process that runs inside WSL.

The entire bridge transport is the stdin/stdout/stderr of one ``wsl --exec``
child process; this module owns how that child is built (argument quoting,
path translation, environment verification) and how it is torn down.
"""
import ctypes
import logging
import os
import shutil
import subprocess

from ..core.constants import CREATE_NO_WINDOW

# -- Windows Job Object: tie the WSL child's life to this host process --------
#
# Closing the console window (the X button / taskkill) terminates the Python
# host *without* running its Ctrl+C cleanup, which would otherwise orphan
# wsl.exe -- and with it the WSL bridge.py. The orphan keeps holding the
# RFC2217/UF2 ports and the injected shell-rc block forever. Putting wsl.exe in
# a Job Object flagged KILL_ON_JOB_CLOSE makes the OS terminate it the moment
# this process exits for any reason, which closes the helper's stdin and lets
# its normal EOF cleanup run (exactly like a Ctrl+C teardown).

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100

# -- Console-neutral probe spawning -------------------------------------------
#
# The short-lived probes below (wslpath translation, the python3/script checks)
# each run a command *inside* the distro, so whichever runs first cold-boots
# the WSL2 VM. If that boot inherits the caller's interactive console and its
# stdin, wsl.exe initialises the VM's terminal relay against them, and that
# state persists for the VM's whole lifetime: every interactive `wsl` opened
# afterwards then comes up with no echo and garbled input, until the next
# `wsl --shutdown`. (Booting the VM from a real interactive `wsl` first avoids
# it -- which is why the bug only appears when com2tty starts the VM.) Detaching
# the probe -- no console window, stdin from the null device -- makes the cold
# boot terminal-neutral, exactly as spawn_wsl_helper already does for the
# long-lived helper. ``creationflags`` is a no-op off Windows.
CONSOLE_NEUTRAL = {
    "stdin": subprocess.DEVNULL,
    "creationflags": CREATE_NO_WINDOW,
}


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _assign_kill_on_close_job(pid, kernel32=None):
    """Put process ``pid`` in a kill-on-close Job Object; return its handle.

    The handle must be kept alive for the host's lifetime -- it is the OS
    closing this handle (on host exit) that triggers the child's termination.
    Returns None if the job could not be set up, so the caller degrades to the
    previous (orphan-on-close) behaviour rather than failing to spawn.
    """
    if not isinstance(pid, int):
        return None
    if kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.OpenProcess.argtypes = [
        ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.AssignProcessToJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
            job, _JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(job)
        return None
    h_proc = kernel32.OpenProcess(
        _PROCESS_TERMINATE | _PROCESS_SET_QUOTA, False, pid)
    if not h_proc:
        kernel32.CloseHandle(job)
        return None
    try:
        if not kernel32.AssignProcessToJobObject(job, h_proc):
            kernel32.CloseHandle(job)
            return None
    finally:
        kernel32.CloseHandle(h_proc)
    return job


def _arm_kill_on_close(proc):
    """Tie the WSL child's lifetime to this host process (Windows only)."""
    if os.name != "nt":
        return
    try:
        job = _assign_kill_on_close_job(proc.pid)
    except Exception as e:  # never let job setup break bridge startup
        logging.debug(f"Could not arm kill-on-close for WSL helper: {e}")
        return
    if job is not None:
        # Stash the handle on the Popen so it lives as long as the host does;
        # the OS closing it on exit is what reaps the orphaned wsl.exe.
        proc._com2tty_kill_job = job


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
                             encoding="utf-8", errors="replace", check=True,
                             **CONSOLE_NEUTRAL)
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
            errors="replace", timeout=30, **CONSOLE_NEUTRAL,
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
            capture_output=True, timeout=30, **CONSOLE_NEUTRAL,
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
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        creationflags=CREATE_NO_WINDOW,
    )
    _arm_kill_on_close(proc)
    return proc


def terminate_wsl_helper(proc, timeout=3.0):
    """Shut the helper down, giving it a chance to clean up first.

    Closing the helper's stdin makes the WSL select loop see EOF and run its
    own teardown (the ~/.bashrc block, picotool interception, tty symlink,
    heartbeats). We then WAIT for that graceful exit before escalating: a
    forced ``terminate()`` (TerminateProcess on wsl.exe) would otherwise kill
    the helper mid-cleanup and leak its injections.
    """
    if proc.poll() is not None:
        return
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except Exception as e:
        logging.debug(f"Could not close WSL helper stdin: {e}")
    # Give the helper room to finish its own cleanup and exit on the EOF.
    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    logging.info("WSL helper did not exit on its own; terminating...")
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        logging.warning("WSL process did not exit. Killing it.")
        proc.kill()
