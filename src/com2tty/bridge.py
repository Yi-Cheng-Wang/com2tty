import sys
import os
import select
import argparse
import signal
import traceback
import termios
import threading
import socket
import glob

PICOTOOL_WRAPPER_CONTENT = """#!/usr/bin/env python3
import sys
import os
import socket

def main():
    args = sys.argv[1:]
    target_file = None
    for arg in args:
        if arg.endswith('.elf') or arg.endswith('.uf2'):
            target_file = arg
            break
            
    if not target_file:
        sys.exit(0)
        
    if target_file.endswith('.elf'):
        uf2_file = target_file[:-4] + '.uf2'
        if not os.path.exists(uf2_file):
            uf2_file = target_file
    else:
        uf2_file = target_file
        
    if not os.path.exists(uf2_file):
        print(f"com2tty UF2 wrapper: {uf2_file} not found.", file=sys.stderr)
        sys.exit(1)
        
    print(f"com2tty UF2 wrapper: Sending {uf2_file} to host...", file=sys.stderr)
    try:
        with open(uf2_file, 'rb') as f:
            data = f.read()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(('127.0.0.1', {port}))
        s.sendall(data)
        s.close()
        print("com2tty UF2 wrapper: Transfer complete.", file=sys.stderr)
    except Exception as e:
        print(f"com2tty UF2 wrapper error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
"""

intercepted_picotools = []

def setup_picotool_interceptor(uf2_port):
    wrapper_path = "/tmp/com2tty_picotool.py"
    try:
        with open(wrapper_path, "w") as f:
            f.write(PICOTOOL_WRAPPER_CONTENT.replace("{port}", str(uf2_port)))
        os.chmod(wrapper_path, 0o755)
    except Exception as e:
        sys.stderr.write(f"Warning: Failed to create picotool wrapper: {e}\n")
        return

    home = os.path.expanduser("~")
    search_pattern = os.path.join(home, ".platformio", "packages", "tool-picotool*", "picotool")
    for picotool_path in glob.glob(search_pattern):
        if os.path.islink(picotool_path) or not os.path.isfile(picotool_path):
            continue
        real_path = picotool_path + ".real"
        try:
            if not os.path.exists(real_path):
                os.rename(picotool_path, real_path)
            if os.path.lexists(picotool_path):
                os.remove(picotool_path)
            os.symlink(wrapper_path, picotool_path)
            intercepted_picotools.append((picotool_path, real_path))
            sys.stderr.write(f"Intercepted picotool at {picotool_path}\n")
        except Exception as e:
            sys.stderr.write(f"Warning: Failed to intercept {picotool_path}: {e}\n")

def cleanup_picotool_interceptor():
    for picotool_path, real_path in intercepted_picotools:
        try:
            if os.path.lexists(picotool_path):
                os.remove(picotool_path)
            if os.path.exists(real_path):
                os.rename(real_path, picotool_path)
            sys.stderr.write(f"Restored picotool at {picotool_path}\n")
        except Exception as e:
            sys.stderr.write(f"Warning: Failed to restore {picotool_path}: {e}\n")

def restore_orphaned_picotools():
    """Restore picotool binaries left intercepted by a previous, crashed session.

    setup_picotool_interceptor renames the real binary to ``picotool.real`` and
    replaces ``picotool`` with a symlink to our wrapper, relying on the in-process
    cleanup to undo it. If the bridge is killed (e.g. ``wsl --shutdown`` or a host
    ``proc.kill()``) before cleanup runs, that swap persists and the user's
    PlatformIO uploads silently break. This runs on startup and reverses any such
    orphaned swap so the tool self-heals on the next launch.
    """
    home = os.path.expanduser("~")
    search_pattern = os.path.join(home, ".platformio", "packages", "tool-picotool*", "picotool.real")
    for real_path in glob.glob(search_pattern):
        picotool_path = real_path[:-len(".real")]
        try:
            # Only reclaim when the live path is gone or is a symlink we left
            # behind; never clobber a genuine binary the user reinstalled.
            if os.path.islink(picotool_path) or not os.path.exists(picotool_path):
                if os.path.lexists(picotool_path):
                    os.remove(picotool_path)
                os.rename(real_path, picotool_path)
                sys.stderr.write(f"Restored orphaned picotool at {picotool_path}\n")
                sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"Warning: could not restore orphaned picotool {picotool_path}: {e}\n")
            sys.stderr.flush()

baud_map = {getattr(termios, k): int(k[1:]) for k in dir(termios) if k.startswith('B') and k[1:].isdigit()}

def md5_hexdigest(data):
    import hashlib
    try:
        return hashlib.md5(data, usedforsecurity=False).hexdigest()
    except TypeError:  # Python < 3.9 has no usedforsecurity flag
        return hashlib.md5(data).hexdigest()

MARKER_START = "# === COM2TTY INJECTION START ==="
MARKER_END   = "# === COM2TTY INJECTION END ==="

def get_rc_files():
    home = os.path.expanduser("~")
    files = [os.path.join(home, ".bashrc")]
    # zsh users never source .bashrc, so the injected PlatformIO variables
    # would silently be missing in their shells.
    zshrc = os.path.join(home, ".zshrc")
    if os.environ.get("SHELL", "").endswith("zsh") or os.path.exists(zshrc):
        files.append(zshrc)
    return files

def clean_rc():
    for rc_path in get_rc_files():
        if not os.path.exists(rc_path):
            continue
        try:
            with open(rc_path, "r") as f:
                lines = f.readlines()
            new_lines = []
            in_block = False
            for line in lines:
                if MARKER_START in line:
                    idx = line.find(MARKER_START)
                    if idx > 0 and line[:idx].strip():
                        new_lines.append(line[:idx] + "\n")
                    in_block = True
                    continue
                if MARKER_END in line:
                    in_block = False
                    continue
                if not in_block:
                    new_lines.append(line)
            with open(rc_path, "w") as f:
                f.writelines(new_lines)
            sys.stderr.write(f"Cleaned injection from {rc_path}\n")
            sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"Warning: could not clean {rc_path}: {e}\n")
            sys.stderr.flush()

def inject_rc(port, monitor_path="/tmp/ttyUSB0"):
    clean_rc()
    block = (
        f"{MARKER_START}\n"
        f"export PLATFORMIO_UPLOAD_PORT=rfc2217://127.0.0.1:{port}\n"
        f"export PLATFORMIO_MONITOR_PORT={monitor_path}\n"
        f"{MARKER_END}\n"
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

def get_pty_settings(fd):
    try:
        attrs = termios.tcgetattr(fd)
        speed = attrs[5]
        baud = baud_map.get(speed)
        cflag = attrs[2]
        cs_mask = termios.CS5 | termios.CS6 | termios.CS7 | termios.CS8
        cs_val = cflag & cs_mask
        bytesize_map = {termios.CS5: 5, termios.CS6: 6, termios.CS7: 7, termios.CS8: 8}
        bytesize = bytesize_map.get(cs_val, 8)
        if cflag & termios.PARENB:
            parity = 'O' if (cflag & termios.PARODD) else 'E'
        else:
            parity = 'N'
        stopbits = '2' if (cflag & termios.CSTOPB) else '1'
        return baud, bytesize, parity, stopbits
    except Exception:
        return None, None, None, None

def cleanup_symlink(path):
    try:
        if os.path.lexists(path):
            os.unlink(path)
            sys.stderr.write(f"Removed symlink {path}\n")
            sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"Warning: Failed to remove symlink {path}: {e}\n")
        sys.stderr.flush()

def kill_leftover_listener(port):
    """Reclaim a TCP port held by a *com2tty* listener from a previous session.

    Only processes whose command line references this bridge are killed, so an
    unrelated service that happens to use the same port is never terminated.
    (The previous implementation ran ``fuser -k`` which killed any owner.)
    """
    import subprocess as sp
    import time
    try:
        res = sp.run(["fuser", f"{port}/tcp"], capture_output=True, timeout=3)
    except FileNotFoundError:
        # Minimal distros ship without psmisc; the bind below will then fail
        # loudly if a leftover listener is still holding the port.
        sys.stderr.write(
            f"Note: 'fuser' not found (install package 'psmisc'); cannot "
            f"auto-clean leftover listeners on port {port}.\n")
        sys.stderr.flush()
        return
    except Exception:
        return

    pids = res.stdout.decode("utf-8", "replace").split()
    killed = False
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except Exception:
            continue
        # Reclaim the port only from another com2tty bridge instance.
        if "bridge.py" in cmdline or "com2tty" in cmdline:
            try:
                os.kill(int(pid), signal.SIGKILL)
                killed = True
            except Exception:
                pass
        else:
            sys.stderr.write(
                f"Note: port {port} is held by an unrelated process (PID {pid}); "
                f"not killing it. Choose a different --rfc2217-port if bind fails.\n")
            sys.stderr.flush()
    if killed:
        time.sleep(0.3)

def run_rfc2217_server_thread(port, rfc2217_active):
    """
    Long-lived TCP forwarder that runs as a thread inside the main bridge process.
    Accepts esptool connections and relays data through stdin/stdout (shared with
    the PTY bridge, coordinated by the rfc2217_active event).
    """
    kill_leftover_listener(port)

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(('127.0.0.1', port))
    except Exception as e:
        sys.stderr.write(
            f"[CONTROL] RFC2217_ERROR: bind failed: {e}. Port {port} may be "
            f"in use; choose another with --rfc2217-port.\n")
        sys.stderr.flush()
        return
    s.listen(1)
    s.settimeout(1.0)

    sys.stderr.write(f"[CONTROL] RFC2217_READY:{port}\n")
    sys.stderr.flush()

    try:
        while True:
            try:
                conn, addr = s.accept()
            except socket.timeout:
                continue
            except Exception:
                break

            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            # Signal connection to Windows side and pause PTY bridge
            sys.stderr.write("[CONTROL] RFC2217_CONNECT\n")
            sys.stderr.flush()
            rfc2217_active.set()
            import time
            time.sleep(0.3)  # Wait for main loop to yield stdin/stdout

            conn.setblocking(False)

            try:
                while True:
                    r, _, _ = select.select([0, conn], [], [], 0.5)
                    if 0 in r:
                        data = os.read(0, 4096)
                        if not data:
                            break
                        conn.sendall(data)
                    if conn in r:
                        try:
                            data = conn.recv(4096)
                            if not data:
                                break
                            os.write(1, data)
                        except BlockingIOError:
                            continue
                        except ConnectionResetError:
                            break
            except Exception as e:
                sys.stderr.write(f"[CONTROL] RFC2217_ERROR: session: {e}\n")
                sys.stderr.flush()
            finally:
                conn.close()

            # Signal disconnection and resume PTY bridge
            rfc2217_active.clear()
            sys.stderr.write("[CONTROL] RFC2217_DISCONNECT\n")
            sys.stderr.flush()
    finally:
        s.close()

def run_uf2_relay_thread(port, uf2_active):
    """
    TCP server inside WSL that receives UF2 data from the picotool wrapper
    and relays it to the Windows host through stdout pipe with control messages.
    """
    import time

    kill_leftover_listener(port)

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(('127.0.0.1', port))
    except Exception as e:
        sys.stderr.write(
            f"[CONTROL] UF2_ERROR: bind failed on port {port}: {e}. The UF2 "
            f"relay uses --rfc2217-port + 1; choose another --rfc2217-port.\n")
        sys.stderr.flush()
        return
    s.listen(1)
    s.settimeout(1.0)

    sys.stderr.write(f"[CONTROL] UF2_READY:{port}\n")
    sys.stderr.flush()

    try:
        while True:
            try:
                conn, addr = s.accept()
            except socket.timeout:
                continue
            except Exception:
                break

            # Read all UF2 data from the picotool wrapper
            uf2_data = bytearray()
            try:
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    uf2_data.extend(chunk)
            except Exception:
                pass
            finally:
                conn.close()

            md5_hash = md5_hexdigest(uf2_data)

            sys.stderr.write(f"[CONTROL] UF2_UPLOAD_START:{len(uf2_data)}:{md5_hash}\n")
            sys.stderr.flush()

            # Pause the PTY main loop so we own stdout exclusively
            uf2_active.set()
            time.sleep(0.3)

            # Block, waiting for [CONTROL] UF2_ACK from stdin (fd 0)
            ack_received = False
            timeout_time = time.time() + 5.0
            buffer = b""
            while time.time() < timeout_time and not ack_received:
                r, _, _ = select.select([0], [], [], 0.1)
                if 0 in r:
                    try:
                        chunk = os.read(0, 1024)
                        if not chunk:
                            break
                        buffer += chunk
                        if b"[CONTROL] UF2_ACK" in buffer:
                            ack_received = True
                            break
                    except Exception:
                        break

            if ack_received:
                # Send UF2 binary data through stdout pipe to Windows host
                try:
                    sys.stdout.buffer.write(uf2_data)
                    sys.stdout.buffer.flush()
                except Exception as e:
                    sys.stderr.write(f"[CONTROL] UF2_ERROR: Failed to write to stdout: {e}\n")
                    sys.stderr.flush()
            else:
                sys.stderr.write("[CONTROL] UF2_ERROR: Timeout waiting for host UF2_ACK\n")
                sys.stderr.flush()

            sys.stderr.write("[CONTROL] UF2_UPLOAD_END\n")
            sys.stderr.flush()

            uf2_active.clear()
    finally:
        s.close()

def main():
    parser = argparse.ArgumentParser(description="com2tty WSL Bridge Helper")
    parser.add_argument(
        "-s", "--symlink",
        required=True,
        help="Target symlink path for the pseudo-terminal device."
    )
    parser.add_argument(
        "-r", "--rfc2217-port",
        type=int,
        help="TCP port for RFC 2217 server to inject into bashrc and listen on"
    )
    args = parser.parse_args()

    target_path = args.symlink
    created_symlink = None

    # We must keep both master and slave descriptors open.
    # Keeping slave_fd open prevents EIO errors on the master side when
    # WSL clients open and close the virtual serial port.
    master_fd = None
    slave_fd = None

    if args.rfc2217_port:
        # Self-heal anything a previous, crashed session left behind before we
        # set up our own interceptors. inject_rc already clears stale rc blocks.
        restore_orphaned_picotools()
        inject_rc(args.rfc2217_port, args.symlink)

    # Events to coordinate stdin/stdout access between PTY bridge, RFC 2217, and UF2 relay
    rfc2217_active = threading.Event()
    uf2_active = threading.Event()

    try:
        master_fd, slave_fd = os.openpty()
        slave_name = os.ttyname(slave_fd)

        sys.stderr.write(f"Created pseudo-terminal: master_fd={master_fd}, slave={slave_name}\n")
        sys.stderr.flush()

        # Attempt to create the symlink at target path
        try:
            if os.path.lexists(target_path):
                os.unlink(target_path)
            os.symlink(slave_name, target_path)
            created_symlink = target_path
            sys.stderr.write(f"Successfully symlinked {target_path} -> {slave_name}\n")
            sys.stderr.flush()
        except PermissionError:
            # Fallback to /tmp if write permission to dev is denied
            basename = os.path.basename(target_path)
            fallback_path = f"/tmp/{basename}"
            sys.stderr.write(f"Warning: Permission denied creating symlink at {target_path}.\n")
            sys.stderr.write(f"Attempting fallback to user-writable path: {fallback_path}...\n")
            sys.stderr.flush()
            
            if os.path.lexists(fallback_path):
                os.unlink(fallback_path)
            os.symlink(slave_name, fallback_path)
            created_symlink = fallback_path
            
            sys.stderr.write(f"Fallback successful: {fallback_path} -> {slave_name}\n")
            sys.stderr.write("--------------------------------------------------\n")
            sys.stderr.write(f"To use the desired device path '{target_path}', please run this command ONCE in WSL:\n")
            sys.stderr.write(f"  sudo ln -sf {fallback_path} {target_path}\n")
            sys.stderr.write("--------------------------------------------------\n")
            sys.stderr.flush()

        # Start RFC 2217 server thread if port is specified
        if args.rfc2217_port:
            uf2_port = args.rfc2217_port + 1
            setup_picotool_interceptor(uf2_port)
            t_rfc2217 = threading.Thread(
                target=run_rfc2217_server_thread,
                args=(args.rfc2217_port, rfc2217_active),
                daemon=True
            )
            t_rfc2217.start()
            t_uf2_relay = threading.Thread(
                target=run_uf2_relay_thread,
                args=(uf2_port, uf2_active),
                daemon=True
            )
            t_uf2_relay.start()
            
        # Select loop
        # 0 is stdin, master_fd is the pseudo-terminal master
        sys.stderr.write("WSL bridge enter main loop.\n")
        sys.stderr.flush()
        
        last_settings = None
        
        while True:
            # Yield stdin/stdout to RFC 2217 forwarder or UF2 relay when active
            if rfc2217_active.is_set() or uf2_active.is_set():
                import time
                time.sleep(0.1)
                continue

            current_settings = get_pty_settings(master_fd)
            if current_settings != last_settings and current_settings[0] is not None:
                baud, bytesize, parity, stopbits = current_settings
                sys.stderr.write(f"[CONTROL] SETTINGS: baud={baud} bytesize={bytesize} parity={parity} stopbits={stopbits}\n")
                sys.stderr.flush()
                last_settings = current_settings

            # select blocks until data is available on stdin or master_fd
            r, w, x = select.select([0, master_fd], [], [], 0.5)

            # Re-check after select returns (RFC 2217 or UF2 might have activated during select)
            if rfc2217_active.is_set() or uf2_active.is_set():
                continue
            
            if 0 in r:
                data = os.read(0, 4096)
                if not data:
                    sys.stderr.write("EOF on stdin. Exiting.\n")
                    sys.stderr.flush()
                    break
                os.write(master_fd, data)
                
            if master_fd in r:
                # Read from the virtual serial port
                try:
                    data = os.read(master_fd, 4096)
                    if not data:
                        # Should not happen typically while slave_fd is kept open,
                        # but handle it gracefully if it does.
                        sys.stderr.write("EOF on PTY master. Exiting.\n")
                        sys.stderr.flush()
                        break
                    os.write(1, data)
                except OSError as e:
                    # In case of EIO (Input/output error) when slave closes,
                    # just ignore it and continue since we keep slave_fd open.
                    if e.errno == 5: # EIO
                        continue
                    else:
                        raise e
                        
    except KeyboardInterrupt: # pragma: no cover
        sys.stderr.write("WSL bridge interrupted by signal.\n")
        sys.stderr.flush()
    except Exception as e: # pragma: no cover
        sys.stderr.write(f"WSL bridge error: {traceback.format_exc()}\n")
        sys.stderr.flush()
    finally:
        # Clean up symlink and file descriptors
        if args.rfc2217_port:
            clean_rc()
            cleanup_picotool_interceptor()
        if created_symlink:
            cleanup_symlink(created_symlink)
        if slave_fd is not None:
            try:
                os.close(slave_fd)
            except Exception:
                pass
        if master_fd is not None:
            try:
                os.close(master_fd)
            except Exception:
                pass
        sys.stderr.write("WSL bridge shut down.\n")
        sys.stderr.flush()

if __name__ == "__main__": # pragma: no cover
    main()
