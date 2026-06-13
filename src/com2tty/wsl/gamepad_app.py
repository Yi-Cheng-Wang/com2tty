"""Entry point of the WSL gamepad helper (spawned via ``pad_bridge.py``).

Reads fixed-length controller frames from stdin (fed by the Windows host,
which polls XInput), feeds them to the selected evdev sink, and forwards
force-feedback (rumble) requests back to the host over stdout.
"""
import argparse
import os
import select
import struct
import sys

from ..core.frames import FRAME_FORMAT, FRAME_MAGIC0, FRAME_MAGIC1
from .evdev_sink import (
    DEFAULT_TMP_PAD,
    FrameReader,
    _open_sink,
    pack_rumble,
    parse_frame,
    state_to_events,
)


def build_arg_parser():
    parser = argparse.ArgumentParser(description="com2tty WSL Gamepad Bridge")
    parser.add_argument("-i", "--pad-index", type=int, default=0,
                        help="Controller index this bridge represents (0-3).")
    parser.add_argument("-n", "--name", default="Microsoft X-Box 360 pad",
                        help="Virtual device name advertised to Linux.")
    parser.add_argument("-u", "--uinput", action="store_true",
                        help="Create a real /dev/input device via /dev/uinput "
                             "(needs one-time root setup). Default is the "
                             "root-free /tmp evdev stream.")
    parser.add_argument("-p", "--tmp-path", default=DEFAULT_TMP_PAD,
                        help="FIFO path for the /tmp stream sink "
                             "(default: %s)." % DEFAULT_TMP_PAD)
    return parser


def main(argv=None):  # pragma: no cover - integration entry point
    args = build_arg_parser().parse_args(argv)

    try:
        pad, ready_msg = _open_sink(args.uinput, args.tmp_path, args.name)
    except Exception as exc:
        sys.stderr.write(f"[CONTROL] PAD_ERROR: cannot init sink: {exc}\n")
        sys.stderr.flush()
        return 13

    sys.stderr.write(ready_msg + "\n")
    sys.stderr.flush()

    reader = FrameReader()
    # Emit an initial neutral state so the device reads as centred/idle.
    pad.emit(state_to_events(parse_frame(struct.pack(
        FRAME_FORMAT, FRAME_MAGIC0, FRAME_MAGIC1, args.pad_index,
        0, 0, 0, 0, 0, 0, 0, 0))))

    # The uinput sink exposes a second readable fd that carries the kernel's
    # force-feedback requests; rumble magnitudes go back to the Windows host
    # over stdout (the reverse channel of the bridge pipe).
    ff_fd = pad.ff_fileno()
    watch = [0] if ff_fd is None else [0, ff_fd]

    try:
        while True:
            readable, _, _ = select.select(watch, [], [])

            if ff_fd is not None and ff_fd in readable:
                rumble = pad.handle_ff_io()
                if rumble is not None:
                    sys.stdout.buffer.write(pack_rumble(*rumble))
                    sys.stdout.buffer.flush()

            if 0 not in readable:
                continue
            data = os.read(0, 4096)
            if not data:
                sys.stderr.write("EOF on stdin. Exiting.\n")
                sys.stderr.flush()
                break
            for state in reader.feed(data):
                if state["index"] != args.pad_index:
                    continue
                pad.emit(state_to_events(state))
    except KeyboardInterrupt:  # pragma: no cover
        pass
    except Exception as exc:  # pragma: no cover
        sys.stderr.write(f"[CONTROL] PAD_ERROR: {exc}\n")
        sys.stderr.flush()
    finally:
        pad.close()
        sys.stderr.write("WSL gamepad bridge shut down.\n")
        sys.stderr.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
