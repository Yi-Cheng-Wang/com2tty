"""Lifecycle manager for the dashboard's background bridges.

``BridgeManager`` is the Controller/Service seam between the TUI (the View in
:mod:`.app`) and the long-running bridge sessions in
:mod:`com2tty.windows.bridge_app` / :mod:`com2tty.windows.gamepad_app`. Each
attached device runs as one daemon thread driving ``run_bridge`` /
``run_gamepad_bridge``; those entry points already cooperate with a
``stop_event`` and return an exit reason, so the manager only has to own the
thread, the stop event, and the observable state.

Because several devices can be attached at once, the manager also owns the
resource allocation the CLI's ``run_multi_bridge`` does up front: every serial
bridge is given a free *slot index*, and from that index a distinct WSL
endpoint (``/tmp/ttyUSB{n}``), a distinct RFC 2217 port (``base + 2*n``, the
``+1`` reserved for the UF2 relay), and the ``env_setup`` flag (only slot 0
injects the shell rc). Detaching a bridge frees its slot for reuse, so the
allocation stays compact as devices come and go.

The manager deliberately knows nothing about Textual: it is a plain
thread-safe object so it can be unit-tested without a terminal. The runner
callables are injectable for the same reason -- tests pass fakes instead of
spawning real WSL helpers.
"""
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from com2tty.core.constants import DEFAULT_RFC2217_PORT, DEFAULT_WSL_TTY
from com2tty.core.util import indexed_path

logger = logging.getLogger(__name__)

# State machine for a managed bridge. "starting"/"running"/"stopping" are the
# live states; "stopped"/"failed" are terminal and mean the worker thread has
# returned. Only the live states count as "attached".
STARTING = "starting"
RUNNING = "running"
STOPPING = "stopping"
STOPPED = "stopped"
FAILED = "failed"

_LIVE_STATES = frozenset({STARTING, RUNNING, STOPPING})

# Endpoint shown for a uinput gamepad bridge. The kernel assigns the actual
# /dev/input/eventN number, so the table shows this device-class label rather
# than the /tmp fallback path; it is replaced with the fallback path only if the
# uinput permission probe shows the bridge will fall back (see GamepadTab).
UINPUT_ENDPOINT_LABEL = "/dev/input/event* (uinput)"


@dataclass
class BridgeRecord:
    """Observable bookkeeping for one attached bridge."""

    bridge_id: str
    kind: str  # "serial" | "gamepad"
    key: str  # the COM port name, or the pad slot index as a string
    label: str
    stop_event: threading.Event
    thread: Optional[threading.Thread] = None
    state: str = STARTING
    error: Optional[str] = None
    exit_reason: Optional[str] = None
    # Allocated resources (serial only; None for gamepads except endpoint).
    index: Optional[int] = None
    endpoint: Optional[str] = None
    rfc2217_port: Optional[int] = None
    # Gamepad only: whether the uinput tier was requested, so the UI can show
    # the right endpoint (a real /dev/input device vs the /tmp fallback).
    use_uinput: bool = False
    # Serial only: the /dev/* symlink the user aliased this bridge's /tmp
    # endpoint to (auto-detected by the dashboard, see find_dev_aliases), or
    # None when no such alias exists. The UI shows it in place of the /tmp path.
    dev_alias: Optional[str] = None
    _slot_freed: bool = field(default=False, repr=False)

    def snapshot(self) -> dict:
        """A plain-dict copy safe to hand to the UI without sharing state."""
        return {
            "bridge_id": self.bridge_id,
            "kind": self.kind,
            "key": self.key,
            "label": self.label,
            "state": self.state,
            "error": self.error,
            "exit_reason": self.exit_reason,
            "index": self.index,
            "endpoint": self.endpoint,
            "rfc2217_port": self.rfc2217_port,
            "use_uinput": self.use_uinput,
            "dev_alias": self.dev_alias,
        }


class BridgeManager:
    """Start, stop, and report on the dashboard's background bridges.

    The bridge id is ``"{kind}:{key}"`` (e.g. ``serial:COM3``,
    ``gamepad:0``), which gives free de-duplication: attaching a device that
    is already live raises :class:`ValueError` instead of starting a second
    redundant session on the same endpoint.
    """

    def __init__(self,
                 serial_runner: Optional[Callable] = None,
                 gamepad_runner: Optional[Callable] = None,
                 respawn_runner: Optional[Callable] = None,
                 rfc2217_base: int = DEFAULT_RFC2217_PORT,
                 wsl_tty_base: str = DEFAULT_WSL_TTY):
        # Imported lazily/by-default so unit tests can inject fakes and so the
        # package import stays cheap.
        if serial_runner is None or gamepad_runner is None or respawn_runner is None:
            from com2tty.windows.bridge_app import run_bridge, run_with_respawn
            from com2tty.windows.gamepad_app import run_gamepad_bridge
            serial_runner = serial_runner or run_bridge
            gamepad_runner = gamepad_runner or run_gamepad_bridge
            respawn_runner = respawn_runner or run_with_respawn
        self._serial_runner = serial_runner
        self._gamepad_runner = gamepad_runner
        self._respawn_runner = respawn_runner
        self._rfc2217_base = rfc2217_base
        self._wsl_tty_base = wsl_tty_base
        self._bridges: Dict[str, BridgeRecord] = {}
        self._used_serial_slots = set()
        self._lock = threading.Lock()

    # -- public API ---------------------------------------------------------

    def start_serial_bridge(self, port, *, baud="auto", board="auto",
                            distro=None, bytesize=8, parity="N", stopbits=1,
                            xonxoff=False, rtscts=False, dsrdtr=False,
                            wait=True, auto_respawn=False, wsl_path=None) -> str:
        """Attach a COM port. Returns the new bridge id.

        The WSL endpoint and RFC 2217 port are allocated automatically from
        the first free slot so several ports can coexist without colliding.
        ``wsl_path`` overrides the default ``/tmp/ttyUSB{n}`` base, so the
        device name (the ``ttyUSB0`` part) is the user's to choose.

        The bridge always serves the pseudo terminal at this user-writable
        ``/tmp`` endpoint. If the user later aliases it under ``/dev`` by hand
        (``sudo ln -sf <endpoint> /dev/<anyname>``), the dashboard auto-detects
        that alias (see :func:`com2tty.windows.doctor.find_dev_aliases` and the
        Serial tab's poll) and records it in ``dev_alias`` for display -- so no
        ``/dev`` name has to be configured up front and any name the user picks
        is found.
        """
        bridge_id = f"serial:{port}"
        with self._lock:
            self._reap_dead()
            self._reject_if_live(bridge_id, f"Serial {port}")
            index = self._allocate_serial_slot()
            # A bare device name (no separator) means "/tmp/<name>" so it is a
            # real /tmp path, not a relative one.
            base = wsl_path or self._wsl_tty_base
            if "/" not in base:
                base = "/tmp/" + base
            wsl_tty = self._indexed_path(base, index)
            rfc2217_port = self._rfc2217_base + 2 * index
            kwargs = dict(
                port=port, baud=baud, wsl_tty=wsl_tty, bytesize=bytesize,
                parity=parity, stopbits=stopbits, xonxoff=xonxoff,
                rtscts=rtscts, dsrdtr=dsrdtr, rfc2217_port=rfc2217_port,
                distro=distro, board=board, wait=wait,
                env_setup=(index == 0),
            )
            record = BridgeRecord(
                bridge_id=bridge_id, kind="serial", key=str(port),
                label=f"Serial {port}", stop_event=threading.Event(),
                index=index, endpoint=wsl_tty, rfc2217_port=rfc2217_port,
            )
            self._launch(record, self._serial_runner, kwargs, auto_respawn)
        logger.info("Attached %s on %s (RFC 2217 port %d).",
                    record.label, wsl_tty, rfc2217_port)
        return bridge_id

    def start_gamepad_bridge(self, pad_index, *, poll_hz=250,
                            name="Microsoft X-Box 360 pad", use_uinput=False,
                            tmp_path=None, distro=None,
                            auto_respawn=False) -> str:
        """Attach an XInput controller slot. Returns the new bridge id.

        The WSL FIFO defaults to ``/tmp/com2pad{pad_index}`` -- distinct per
        slot -- so multiple controllers never share an endpoint.
        """
        if tmp_path is None:
            tmp_path = f"/tmp/com2pad{pad_index}"
        bridge_id = f"gamepad:{pad_index}"
        with self._lock:
            self._reap_dead()
            self._reject_if_live(bridge_id, f"Gamepad slot {pad_index}")
            kwargs = dict(
                pad_index=pad_index, poll_hz=poll_hz, name=name,
                use_uinput=use_uinput, tmp_path=tmp_path, distro=distro,
            )
            # Show the real uinput device class up front; the /tmp fallback is
            # only substituted later if the permission probe says it will fall
            # back (so a successful uinput bridge never shows the /tmp path).
            endpoint = UINPUT_ENDPOINT_LABEL if use_uinput else tmp_path
            record = BridgeRecord(
                bridge_id=bridge_id, kind="gamepad", key=str(pad_index),
                label=f"Gamepad slot {pad_index}",
                stop_event=threading.Event(), endpoint=endpoint,
                use_uinput=use_uinput,
            )
            self._launch(record, self._gamepad_runner, kwargs, auto_respawn)
        logger.info("Attached %s on %s.", record.label, endpoint)
        return bridge_id

    def stop_bridge(self, bridge_id, timeout=5.0) -> bool:
        """Detach a bridge by id; blocks until its thread joins or times out.

        Returns False if no such bridge is being tracked. The worker thread
        frees the bridge's serial slot as it unwinds, so by the time this
        returns the slot is available for a new attach.
        """
        with self._lock:
            record = self._bridges.get(bridge_id)
            if record is None:
                return False
            record.state = STOPPING
            record.stop_event.set()
            thread = record.thread

        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

        with self._lock:
            self._bridges.pop(bridge_id, None)
        return True

    def stop_all(self, timeout=5.0) -> None:
        """Detach every bridge (used on dashboard shutdown)."""
        for bridge_id in self.list_bridge_ids():
            self.stop_bridge(bridge_id, timeout=timeout)

    def is_attached(self, kind, key) -> bool:
        """True while a ``{kind}:{key}`` bridge is still live."""
        bridge_id = f"{kind}:{key}"
        with self._lock:
            self._reap_dead()
            record = self._bridges.get(bridge_id)
            return record is not None and record.state in _LIVE_STATES

    def list_bridges(self) -> List[dict]:
        """Snapshots of every live bridge, for the UI to render.

        Terminal (stopped/failed) records are reaped first, so a bridge that
        ended on its own neither lingers in memory nor surfaces a stale
        endpoint in the device table.
        """
        with self._lock:
            self._reap_dead()
            return [record.snapshot() for record in self._bridges.values()]

    def attached_counts(self) -> Dict[str, int]:
        """Number of currently-live bridges per kind (e.g. ``{"serial": 2}``).

        Keeps the live-state definition inside the manager so the UI can show
        an attached-device summary without re-implementing it.
        """
        counts: Dict[str, int] = {}
        with self._lock:
            self._reap_dead()
            for record in self._bridges.values():
                if record.state in _LIVE_STATES:
                    counts[record.kind] = counts.get(record.kind, 0) + 1
        return counts

    def list_bridge_ids(self) -> List[str]:
        with self._lock:
            return list(self._bridges.keys())

    def get(self, bridge_id) -> Optional[dict]:
        with self._lock:
            record = self._bridges.get(bridge_id)
            return record.snapshot() if record is not None else None

    def set_endpoint(self, bridge_id, endpoint) -> bool:
        """Update a tracked bridge's displayed endpoint.

        Called (from a worker thread) when the gamepad uinput permission probe
        shows the tier will fall back to the /tmp stream, so the Endpoint column
        reflects the real sink instead of the optimistic one. Returns False when
        the bridge is no longer tracked.
        """
        with self._lock:
            record = self._bridges.get(bridge_id)
            if record is None:
                return False
            record.endpoint = endpoint
            return True

    def set_dev_alias(self, bridge_id, dev_alias) -> bool:
        """Record (or clear) the auto-detected ``/dev`` alias of a serial bridge.

        Called from the Serial tab's periodic poll when it discovers (or loses)
        a ``/dev/*`` symlink that the user aliased the bridge's ``/tmp`` endpoint
        to. The displayed endpoint then shows the ``/dev`` path while it resolves
        and reverts to ``/tmp`` when it is removed. Returns False (no change)
        when the value is unchanged or the bridge is no longer tracked, so the
        caller can tell whether a repaint is needed.
        """
        with self._lock:
            record = self._bridges.get(bridge_id)
            if record is None or record.dev_alias == dev_alias:
                return False
            record.dev_alias = dev_alias
            return True

    # -- internals ----------------------------------------------------------

    def _reap_dead(self) -> None:
        """Drop records that have reached a terminal state. Caller holds lock.

        A bridge that ends on its own (WSL exited, or its runner raised) leaves
        a STOPPED/FAILED record behind. Its slot was already released by the
        worker, so dropping the bookkeeping is all that remains; without it the
        records accumulate unboundedly and keep showing a stale endpoint.
        ``get`` deliberately does not reap, so a caller can still read the exit
        reason of a specific bridge right after it stops.
        """
        for bridge_id in [bid for bid, rec in self._bridges.items()
                          if rec.state in (STOPPED, FAILED)]:
            del self._bridges[bridge_id]

    def _reject_if_live(self, bridge_id, label):
        """Caller must hold the lock."""
        existing = self._bridges.get(bridge_id)
        if existing is not None and existing.state in _LIVE_STATES:
            raise ValueError(f"{label} is already attached")

    def _allocate_serial_slot(self) -> int:
        """Lowest free slot index. Caller must hold the lock."""
        index = 0
        while index in self._used_serial_slots:
            index += 1
        self._used_serial_slots.add(index)
        return index

    def _free_serial_slot(self, record) -> None:
        """Release a record's slot exactly once. Caller must hold the lock."""
        if record.index is not None and not record._slot_freed:
            self._used_serial_slots.discard(record.index)
            record._slot_freed = True

    @staticmethod
    def _indexed_path(base, index) -> str:
        """Per-slot WSL path, via the shared :func:`core.util.indexed_path`.

        Keeps the dashboard and the ``run_multi_bridge`` CLI path laying
        endpoints out identically (/tmp/ttyUSB0 -> /tmp/ttyUSB1).
        """
        return indexed_path(base, index)

    def _launch(self, record, runner, kwargs, auto_respawn) -> None:
        """Register the record and start its worker. Caller must hold lock."""
        stop_event = record.stop_event

        def _target():
            # Blocks until the bridge ends (stop requested, WSL exit, or
            # error). Updates the record's terminal state, then frees its
            # slot, so the UI reflects why a bridge dropped and the slot can
            # be reused. This worker is the single point that releases a slot.
            try:
                if auto_respawn:
                    reason = self._respawn_runner(runner, stop_event=stop_event,
                                                  **kwargs)
                else:
                    reason = runner(stop_event=stop_event, **kwargs)
                with self._lock:
                    record.exit_reason = reason
                    if record.state in _LIVE_STATES:
                        record.state = STOPPED
                    self._free_serial_slot(record)
            except Exception as exc:  # noqa: BLE001 - surface any failure
                logger.error("%s stopped: %s", record.label, exc)
                with self._lock:
                    record.state = FAILED
                    record.error = str(exc)
                    self._free_serial_slot(record)

        thread = threading.Thread(target=_target, name=f"bridge-{record.bridge_id}",
                                  daemon=True)
        record.thread = thread
        self._bridges[record.bridge_id] = record
        # Start while still holding the lock: _target only touches the lock
        # once its runner returns, so it cannot overwrite state before we
        # promote the record to RUNNING below.
        thread.start()
        if record.state == STARTING:
            record.state = RUNNING
