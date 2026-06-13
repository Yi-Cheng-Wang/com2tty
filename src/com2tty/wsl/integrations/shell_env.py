"""PlatformIO environment-variable injection into the user's shell rc files.

A bridge session exports ``PLATFORMIO_UPLOAD_PORT``/``PLATFORMIO_MONITOR_PORT``
by appending a marker-delimited block to ``~/.bashrc`` (and ``~/.zshrc``
when zsh is in use), plus a snippet under ``~/.config/fish/conf.d/`` for
fish. Blocks are tagged with the owning session's PID so two concurrent
sessions do not remove each other's configuration, and stale blocks left by
crashed sessions are reclaimed on the next injection.
"""
import os
import stat
import sys
import tempfile

from ...core.constants import (
    RC_MARKER_END_PREFIX as MARKER_END_PREFIX,
    RC_MARKER_START_PREFIX as MARKER_START_PREFIX,
)
from ..liveness import pid_alive


def _atomic_write_lines(path, lines):
    """Replace ``path`` with ``lines`` atomically, preserving its mode.

    Rewriting a real shell rc (``~/.bashrc``/``~/.zshrc``) in place with mode
    ``"w"`` truncates it to zero before the new contents are written; a crash
    or ``wsl --shutdown`` in that window leaves the user with an empty rc.
    Instead, write a sibling temp file, flush+fsync it, then ``os.replace`` it
    over the original so the rc is never observed half-written.
    """
    dir_name = os.path.dirname(path) or "."
    try:
        orig_mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        orig_mode = None
    fd, tmp = tempfile.mkstemp(dir=dir_name, prefix=".com2tty-rc-")
    try:
        with os.fdopen(fd, "w") as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        if orig_mode is not None:
            os.chmod(tmp, orig_mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def marker_start(pid=None):
    pid = os.getpid() if pid is None else pid
    return f"{MARKER_START_PREFIX} [pid={pid}] ==="


def marker_end():
    return f"{MARKER_END_PREFIX} ==="


def _marker_pid(line):
    """Extract the session PID from a start-marker line, or None (legacy)."""
    start = line.find("[pid=")
    if start == -1:
        return None
    end = line.find("]", start)
    if end == -1:
        return None
    try:
        return int(line[start + len("[pid="):end])
    except ValueError:
        return None


def _block_is_removable(line, own_pid):
    """Decide whether the block starting at this marker line may be removed.

    With ``own_pid`` None every block is removed (full cleanup). Otherwise a
    block is removed when it is legacy/untagged, owned by this session, or
    owned by a session that is no longer running.
    """
    if own_pid is None:
        return True
    pid = _marker_pid(line)
    return pid is None or pid == own_pid or not pid_alive(pid)


def get_rc_files():
    home = os.path.expanduser("~")
    files = [os.path.join(home, ".bashrc")]
    # zsh users never source .bashrc, so the injected PlatformIO variables
    # would silently be missing in their shells.
    zshrc = os.path.join(home, ".zshrc")
    if os.environ.get("SHELL", "").endswith("zsh") or os.path.exists(zshrc):
        files.append(zshrc)
    return files


def get_fish_conf_path():
    """Path for the fish snippet, or None when fish is not in use.

    fish does not read .bashrc/.zshrc; files in ~/.config/fish/conf.d/ are
    sourced automatically by every new fish shell, so a dedicated snippet
    there is the idiomatic equivalent of the rc-file block.
    """
    home = os.path.expanduser("~")
    fish_dir = os.path.join(home, ".config", "fish")
    if os.environ.get("SHELL", "").endswith("fish") or os.path.isdir(fish_dir):
        return os.path.join(fish_dir, "conf.d", "com2tty.fish")
    return None


def clean_fish_conf(own_pid=None):
    path = get_fish_conf_path()
    if not path or not os.path.exists(path):
        return
    if own_pid is not None:
        # The fish snippet records its owning session; do not delete a live
        # other session's snippet.
        owner = None
        try:
            with open(path, "r") as f:
                owner = _marker_pid(f.readline())
        except OSError:
            pass
        if owner is not None and owner != own_pid and pid_alive(owner):
            return
    try:
        os.remove(path)
        sys.stderr.write(f"Removed {path}\n")
        sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"Warning: could not remove {path}: {e}\n")
        sys.stderr.flush()


def inject_fish_conf(port, monitor_path="/tmp/ttyUSB0"):
    path = get_fish_conf_path()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(
                f"# Written by com2tty [pid={os.getpid()}]; removed "
                "automatically when it exits.\n"
                f"set -gx PLATFORMIO_UPLOAD_PORT rfc2217://127.0.0.1:{port}\n"
                f"set -gx PLATFORMIO_MONITOR_PORT {monitor_path}\n"
            )
        sys.stderr.write(f"Injected environment variables to {path}\n")
        sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"Warning: could not write {path}: {e}\n")
        sys.stderr.flush()


def clean_rc(own_pid=None):
    """Remove injected environment blocks from the shell rc files.

    With ``own_pid`` set, only this session's block, legacy untagged blocks,
    and blocks left behind by dead sessions are removed; a block owned by a
    different live session is preserved (two independent com2tty sessions
    previously destroyed each other's configuration here).
    """
    clean_fish_conf(own_pid)
    for rc_path in get_rc_files():
        if not os.path.exists(rc_path):
            continue
        try:
            with open(rc_path, "r") as f:
                lines = f.readlines()
            new_lines = []
            in_block = False
            keep_block = False
            for line in lines:
                if MARKER_START_PREFIX in line and not in_block:
                    idx = line.find(MARKER_START_PREFIX)
                    in_block = True
                    keep_block = not _block_is_removable(line, own_pid)
                    if keep_block:
                        new_lines.append(line)
                    elif idx > 0 and line[:idx].strip():
                        new_lines.append(line[:idx] + "\n")
                    continue
                if MARKER_END_PREFIX in line and in_block:
                    in_block = False
                    if keep_block:
                        new_lines.append(line)
                    keep_block = False
                    continue
                if not in_block or keep_block:
                    new_lines.append(line)
            _atomic_write_lines(rc_path, new_lines)
            sys.stderr.write(f"Cleaned injection from {rc_path}\n")
            sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"Warning: could not clean {rc_path}: {e}\n")
            sys.stderr.flush()


def inject_rc(port, monitor_path="/tmp/ttyUSB0"):
    clean_rc(own_pid=os.getpid())
    inject_fish_conf(port, monitor_path)
    block = (
        f"{marker_start()}\n"
        f"export PLATFORMIO_UPLOAD_PORT=rfc2217://127.0.0.1:{port}\n"
        f"export PLATFORMIO_MONITOR_PORT={monitor_path}\n"
        f"{marker_end()}\n"
    )
    for rc_path in get_rc_files():
        try:
            prefix = ""
            if os.path.exists(rc_path):
                with open(rc_path, "r") as f:
                    content = f.read()
                    if content and not content.endswith("\n"):
                        prefix = "\n"
            with open(rc_path, "a") as f:
                f.write(prefix + block)
            sys.stderr.write(f"Injected environment variables to {rc_path}\n")
            sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"Warning: could not inject into {rc_path}: {e}\n")
            sys.stderr.flush()
