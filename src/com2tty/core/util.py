"""Small dependency-free helpers shared by both interpreters.

These live in ``core`` because the Windows host and the WSL helper both need
them and ``core`` is the only layer both sides import. They use the standard
library only (so the WSL side stays dependency-free) and are pure functions,
testable anywhere.
"""
import hashlib
import re


def md5_hexdigest(data):
    """MD5 of ``data`` as a hex string, used only to verify a UF2 transfer.

    ``usedforsecurity=False`` documents that this is an integrity check of an
    image the user is deliberately flashing, not a security primitive; it also
    keeps the call working on FIPS builds. The flag is Python 3.9+, so fall
    back for the 3.8 interpreters the project still supports.
    """
    try:
        return hashlib.md5(data, usedforsecurity=False).hexdigest()
    except TypeError:  # Python < 3.9 has no usedforsecurity flag
        return hashlib.md5(data).hexdigest()


def indexed_path(base, index):
    """Per-slot endpoint path: increment a trailing number, else append index.

    Used identically by the multi-port CLI bridge and the dashboard manager so
    they lay endpoints out the same way (``/tmp/ttyUSB0`` -> ``/tmp/ttyUSB1``,
    ``/tmp/com2pad0`` -> ``/tmp/com2pad1``). Index 0 keeps ``base`` verbatim.
    """
    if index == 0:
        return base
    match = re.match(r"^(.*?)(\d+)$", base)
    if match:
        return f"{match.group(1)}{int(match.group(2)) + index}"
    return f"{base}{index}"
