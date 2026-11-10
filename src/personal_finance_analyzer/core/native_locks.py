"""Process-wide locks for native libraries that are not safe to call from several threads.

FastAPI serves sync endpoints from a threadpool and the `JobRunner` has its own worker thread, so
anything that crosses into native code (pdfium, the macOS Security framework behind `keyring`)
must be serialised. A crash in those libraries is a hard `SIGTRAP`/`SIGSEGV` that kills the whole
server, not a Python exception.
"""

from __future__ import annotations

import threading

#: Every pypdfium2 call (open, render, close, and the finalizers we trigger explicitly) happens
#: while holding this lock.
PDFIUM_LOCK = threading.Lock()

#: Every `keyring.get_password`/`set_password`/`delete_password` call happens while holding this
#: lock (the macOS backend calls Security.framework through ctypes).
KEYRING_LOCK = threading.Lock()
