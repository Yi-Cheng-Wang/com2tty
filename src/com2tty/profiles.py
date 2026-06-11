"""Named argument profiles for the com2tty command line.

``com2tty @myboard`` expands the ``[myboard]`` section of an INI file into
command-line arguments, so a long invocation can be saved once and reused.
The file is plain ``configparser`` INI (stdlib, works on every supported
Python), searched first in the current directory, then in the user's home::

    # com2tty.ini  (or ~/.com2tty.ini)
    [myboard]
    port = COM5
    baud = 115200
    wsl-tty = /tmp/my_device
    board = pico

    [pad]
    gamepad = true
    uinput = true

Keys are the long option names (dashes and underscores both accepted);
``port`` supplies the positional argument. Boolean keys take true/false.
Arguments given on the command line after the ``@profile`` token override
the profile's values.
"""
import configparser
import os

# Options that are argparse store_true flags rather than value options.
FLAG_OPTIONS = frozenset({
    "gamepad", "uinput", "xonxoff", "rtscts", "dsrdtr", "debug", "list",
})

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class ProfileError(Exception):
    """A profile could not be found or is malformed."""


def default_search_paths():
    return [
        os.path.join(os.getcwd(), "com2tty.ini"),
        os.path.join(os.path.expanduser("~"), ".com2tty.ini"),
    ]


def load_profile_args(name, search_paths=None):
    """Resolve a profile name to a list of command-line argument tokens."""
    paths = search_paths if search_paths is not None else default_search_paths()
    parser = configparser.ConfigParser()
    try:
        found = parser.read(paths)
    except configparser.Error as exc:
        raise ProfileError(f"could not parse the profile file: {exc}")
    if not found:
        raise ProfileError(
            "no profile file found (looked for: %s)" % ", ".join(paths))
    if not parser.has_section(name):
        raise ProfileError(
            "profile [%s] not found in %s (available: %s)"
            % (name, ", ".join(found),
               ", ".join(parser.sections()) or "none"))

    args = []
    port = None
    for key, value in parser.items(name):
        option = key.replace("_", "-").lower()
        value = value.strip()
        if option == "port":
            port = value
        elif option in FLAG_OPTIONS:
            lowered = value.lower()
            if lowered in _TRUE_VALUES:
                args.append(f"--{option}")
            elif lowered not in _FALSE_VALUES:
                raise ProfileError(
                    f"profile [{name}]: '{key}' must be a boolean, got '{value}'")
        else:
            args.extend([f"--{option}", value])
    if port is not None:
        args.insert(0, port)
    return args


def expand_profiles(argv, search_paths=None):
    """Replace every ``@name`` token in argv with that profile's arguments."""
    expanded = []
    for token in argv:
        if token.startswith("@") and len(token) > 1:
            expanded.extend(load_profile_args(token[1:], search_paths))
        else:
            expanded.append(token)
    return expanded
