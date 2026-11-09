"""Facts about running inside a container, and the ``NAME`` / ``NAME_FILE`` secret convention.

Docker (and Kubernetes) hand a secret to a container as a file under ``/run/secrets/``, not as an
environment variable that ``docker inspect`` would show. The convention used by the official
images is ``NAME_FILE=/run/secrets/x`` next to the plain ``NAME=value``; `read_env_or_file`
implements it for every deployment secret this app reads (the LLM API key, the login password).
"""

from __future__ import annotations

import os
from pathlib import Path

from personal_finance_analyzer.core.errors import PfaError

#: Set to a true value by the Docker image; forces `running_in_container` on (or off with ``0``).
CONTAINER_ENV = "PFA_CONTAINER"

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


class SecretFileError(PfaError):
    """``NAME_FILE`` names a file that cannot be read, or both ``NAME`` and ``NAME_FILE`` are set.
    The message names the variable and the cause, never the secret."""


def env_flag(name: str, default: bool = False) -> bool:
    """A boolean environment variable (``1/true/yes/on`` and ``0/false/no/off``, any case)."""
    raw = os.environ.get(name, "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


def running_in_container() -> bool:
    """Whether this process runs inside a container: `PFA_CONTAINER` if set, else the marker
    files Docker (``/.dockerenv``) and Podman (``/run/.containerenv``) create."""
    forced = os.environ.get(CONTAINER_ENV, "").strip().lower()
    if forced in _TRUE:
        return True
    if forced in _FALSE:
        return False
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def read_env_or_file(name: str) -> str | None:
    """The value of ``$NAME``, or the contents of the file named by ``$NAME_FILE``; None when
    neither is set (or the value is empty). Only trailing newlines are stripped from file
    contents (``echo secret > file`` leaves one); nothing else is trimmed.

    Raises:
        SecretFileError: both are set, or the file cannot be read.
    """
    direct = os.environ.get(name)
    file_var = f"{name}_FILE"
    file_name = os.environ.get(file_var)
    if direct and file_name:
        raise SecretFileError(f"set either {name} or {file_var}, not both")
    if direct:
        return direct
    if not file_name:
        return None
    try:
        text = Path(file_name).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SecretFileError(f"{file_var}={file_name} cannot be read ({exc})") from None
    value = text.rstrip("\r\n")
    return value or None
