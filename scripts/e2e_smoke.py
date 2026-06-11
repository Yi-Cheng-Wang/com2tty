"""Manual end-to-end smoke test for com2tty on a real Windows + WSL machine.

This is NOT run in CI (CI machines have no WSL or serial hardware). Run it
before a release, from the repository root on a Windows host with WSL and a
USB-serial device attached::

    python scripts/e2e_smoke.py --port COM17
    python scripts/e2e_smoke.py --port COM17 --distro Ubuntu-22.04 --loopback

Checks performed:

1. ``com2tty --list`` runs and mentions the chosen port.
2. A bridge for the port starts and reports readiness.
3. The serial symlink appears inside WSL.
4. The RFC 2217 forwarder accepts a TCP connection inside WSL.
5. With ``--loopback`` (TX wired to RX on the device side), bytes written to
   the WSL endpoint are read back.

Exit code 0 means every check passed.
"""
import argparse
import os
import subprocess
import sys
import threading
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def wsl_cmd(distro, *argv):
    cmd = ["wsl"]
    if distro:
        cmd += ["-d", distro]
    cmd.append("--exec")
    cmd.extend(argv)
    return cmd


def run_step(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""))
    return ok


def check_list(port):
    res = subprocess.run(
        [sys.executable, "-m", "com2tty", "--list"],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=30)
    ok = res.returncode == 0 and port in res.stdout
    return run_step(f"--list shows {port}", ok, res.stdout.strip().splitlines()[0]
                    if res.stdout.strip() else res.stderr.strip()[:120])


def check_symlink(distro, wsl_tty, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = subprocess.run(wsl_cmd(distro, "test", "-L", wsl_tty),
                             capture_output=True, timeout=15)
        if res.returncode == 0:
            return run_step(f"WSL symlink {wsl_tty} exists", True)
        time.sleep(0.5)
    return run_step(f"WSL symlink {wsl_tty} exists", False,
                    f"did not appear within {timeout:.0f}s")


def check_rfc2217(distro, port, timeout=15.0):
    probe = ("import socket,sys\n"
             "s = socket.socket()\n"
             "s.settimeout(3)\n"
             f"s.connect(('127.0.0.1', {port}))\n"
             "s.close()\n")
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = subprocess.run(wsl_cmd(distro, "python3", "-c", probe),
                             capture_output=True, timeout=15)
        if res.returncode == 0:
            return run_step(f"RFC 2217 forwarder reachable on :{port}", True)
        time.sleep(0.5)
    return run_step(f"RFC 2217 forwarder reachable on :{port}", False)


def check_loopback(distro, wsl_tty):
    probe = (
        "import os, select, sys\n"
        f"fd = os.open('{wsl_tty}', os.O_RDWR)\n"
        "os.write(fd, b'com2tty-smoke')\n"
        "r, _, _ = select.select([fd], [], [], 5.0)\n"
        "sys.exit(0 if r and b'com2tty' in os.read(fd, 64) else 1)\n")
    res = subprocess.run(wsl_cmd(distro, "python3", "-c", probe),
                         capture_output=True, timeout=30)
    return run_step("loopback echo through the bridge", res.returncode == 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", required=True, help="COM port to test, e.g. COM17")
    ap.add_argument("--distro", default=None, help="WSL distribution to use")
    ap.add_argument("--wsl-tty", default="/tmp/ttyUSB0")
    ap.add_argument("--rfc2217-port", type=int, default=4000)
    ap.add_argument("--loopback", action="store_true",
                    help="Device has TX wired to RX; verify an echo round-trip.")
    args = ap.parse_args()

    results = [check_list(args.port)]

    bridge_cmd = [sys.executable, "-m", "com2tty", args.port,
                  "--wsl-tty", args.wsl_tty,
                  "--rfc2217-port", str(args.rfc2217_port)]
    if args.distro:
        bridge_cmd += ["--distro", args.distro]
    print(f"Starting bridge: {' '.join(bridge_cmd)}")
    bridge = subprocess.Popen(bridge_cmd, cwd=REPO_ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    # Relay bridge output so failures are debuggable.
    def _pump():
        for line in iter(bridge.stdout.readline, b""):
            sys.stdout.write("  bridge| " + line.decode("utf-8", "replace"))
    threading.Thread(target=_pump, daemon=True).start()

    try:
        results.append(check_symlink(args.distro, args.wsl_tty))
        results.append(check_rfc2217(args.distro, args.rfc2217_port))
        if args.loopback:
            results.append(check_loopback(args.distro, args.wsl_tty))
        results.append(run_step("bridge process still running",
                                bridge.poll() is None))
    finally:
        bridge.terminate()
        try:
            bridge.wait(timeout=10)
        except subprocess.TimeoutExpired:
            bridge.kill()

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
