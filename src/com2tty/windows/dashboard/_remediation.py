"""Copy-pasteable remediation commands for permission/setup deficiencies.

When the dashboard detects something the user must fix once, by hand, inside
WSL -- a permission gap or a missing package -- it surfaces the exact commands
in a :class:`~._screens.CommandHelpScreen` (one-click clipboard copy) rather
than burying them in the scrolling log or expecting the user to dig them out of
the README. The command-line modes already print these to stderr, so this is
the dashboard's equivalent.

Each topic is an ``(title, explanation, commands)`` triple.
"""

# /dev/uinput not writable -> the --uinput gamepad tier falls back to the /tmp
# stream until this one-time root setup is done.
UINPUT_SETUP = (
    "Enable /dev/uinput (one-time root setup)",
    "The --uinput tier needs a writable /dev/uinput. Run these once inside WSL, "
    "then DETACH AND RE-ATTACH the gamepad for it to take effect -- the change "
    "only applies to a freshly started bridge (com2tty itself never needs root "
    "at runtime):",
    [
        "sudo modprobe uinput",
        "sudo chmod 0666 /dev/uinput",
        'sudo usermod -aG input "$USER"',
    ],
)

# fuser missing -> leftover-listener cleanup is disabled.
PSMISC_SETUP = (
    "Install psmisc (provides fuser)",
    "Leftover TCP-listener cleanup uses 'fuser' from the psmisc package; "
    "install it inside WSL:",
    ["sudo apt install psmisc"],
)

# python3 missing in the selected distribution -> the WSL helper cannot run.
PYTHON3_SETUP = (
    "Install python3 in WSL",
    "The WSL helper needs python3 on PATH in the selected distribution:",
    ["sudo apt install python3"],
)

def serial_dev_link(fallback, target):
    """Remediation triple for exposing the serial device at a ``/dev`` path.

    Unlike the static topics above, the exact paths depend on the bridge's
    allocated endpoint, so this is built per-attach. ``com2tty`` keeps the
    ``/tmp`` link pointed at the live pseudo terminal on every start, so the
    one-time ``/dev`` alias keeps resolving.
    """
    name = target.rsplit("/", 1)[-1]
    return (
        f"Expose the serial device at {target}",
        f"{target} is owned by root, so com2tty links the device under the "
        f"user-writable /tmp instead. Run this once inside WSL to alias the "
        f"/dev path to it (re-running is harmless). The '{name}' part is yours "
        f"to choose -- set a different WSL path in Advanced settings to rename "
        f"it:",
        [f"sudo ln -sf {fallback} {target}"],
    )


# Doctor-check label substrings -> the topic that fixes them. Ordered so the
# Doctor tab offers fixes in a stable order.
_LABEL_MATCHERS = (
    ("uinput", UINPUT_SETUP),
    ("fuser", PSMISC_SETUP),
    ("python3", PYTHON3_SETUP),
)


def remediation_for_results(results):
    """De-duplicated remediation topics for the WARN/FAIL doctor results.

    ``results`` is the ``(status, label, detail)`` list from
    :func:`com2tty.windows.doctor.collect_doctor_results`. Returns the
    ``(title, explanation, commands)`` triples whose check failed or warned and
    has a known one-line fix, so the Doctor tab can offer them for copy-paste.
    """
    topics = []
    for status, label, _detail in results:
        if status not in ("WARN", "FAIL"):
            continue
        for needle, topic in _LABEL_MATCHERS:
            if needle in label and topic not in topics:
                topics.append(topic)
    return topics
