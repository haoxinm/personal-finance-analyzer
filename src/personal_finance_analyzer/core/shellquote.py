"""Quote arguments for a copy-pasteable command line in the user's own shell."""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Sequence


def is_windows() -> bool:
    return os.name == "nt"


def quote_arg(arg: str) -> str:
    """Quote one argument for the platform's shell (cmd/PowerShell on Windows, POSIX otherwise)."""
    if is_windows():
        return subprocess.list2cmdline([arg])
    return shlex.quote(arg)


def join_command(args: Sequence[str]) -> str:
    """Join `args` into one command line, each argument quoted for the platform's shell."""
    return " ".join(quote_arg(a) for a in args)
