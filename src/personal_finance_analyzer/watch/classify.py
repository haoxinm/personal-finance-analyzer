"""Where a PDF sits in the documents folder tells which person and which kind of document it is.

Pure functions, no disk and no database: the scan passes in the people it knows.

Person folders are matched to people by name, ignoring case, surrounding and repeated whitespace
and Unicode form (a folder made on macOS is often NFD, one made by the app is NFC). The app's own
folder names come from `core.doc_names.person_folder`, so both the person's name as typed and that
sanitized folder name are accepted: a person called ``A/B`` has the folder ``A-B``, and either
matches. Unknown people are never created from a folder name.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from personal_finance_analyzer.core import doc_names

STATEMENTS_FOLDER = "statements"
TAX_FOLDER = "tax"

Kind = Literal["statement", "tax"]
Status = Literal["ok", "unknown_person", "ambiguous_person", "shared", "unrecognised"]

#: The first/last tax year the tax parsers accept (`ingest.tax.common`).
_MIN_YEAR = 2000
_MAX_YEAR = 2099
_YEAR = re.compile(r"\d{4}")


def normalize_name(text: str) -> str:
    """Case-, whitespace- and Unicode-insensitive form of a name, for comparing folder names."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def person_keys(name: str) -> frozenset[str]:
    """Every folder name a person is accepted under: their name, and the folder the app makes."""
    return frozenset({normalize_name(name), normalize_name(doc_names.person_folder(name))})


@dataclass(frozen=True)
class PathClass:
    """What the location of one PDF means. ``status != "ok"`` means the file cannot be imported
    yet; ``code``/``message``/``hint`` then say why and what to do (never document text)."""

    status: Status
    kind: Kind | None = None
    #: The folder name as it is on disk (the top-level component), if the path has one.
    person_folder: str | None = None
    user_id: int | None = None
    person_name: str | None = None
    #: The ``<year>`` folder under ``Tax/``, when there is one.
    folder_year: int | None = None
    message: str = ""
    hint: str = ""

    @property
    def code(self) -> str | None:
        """The failure code recorded for this classification (``None`` when it is fine)."""
        return _CODES.get(self.status)


_CODES: dict[Status, str] = {
    "unknown_person": "unknown_person",
    "ambiguous_person": "ambiguous_person",
    "shared": "shared_needs_person",
    "unrecognised": "not_recognised_folder",
}

#: Failure codes that depend only on the path and the people (they resolve when a person is
#: created or a folder is renamed, so the scan looks at such a file again without a Retry).
PATH_CODES = frozenset(_CODES.values())

_LAYOUT_HINT = "Put it in <Person>/Statements/ or <Person>/Tax/<year>/ inside the documents folder."


def classify_path(parts: Sequence[str], people: Sequence[tuple[int, str]]) -> PathClass:
    """Classify the file at ``parts`` (path components relative to the documents folder, the last
    one being the file name) given ``people`` as ``(id, name)`` pairs."""
    if len(parts) < 3:
        where = "directly in the documents folder" if len(parts) == 1 else "outside a type folder"
        return PathClass(
            status="unrecognised",
            person_folder=parts[0] if len(parts) > 1 else None,
            message=f"This PDF is {where}, so the app cannot tell whose it is or what it is.",
            hint=_LAYOUT_HINT,
        )
    folder, type_folder = parts[0], parts[1]
    type_key = normalize_name(type_folder)
    kind: Kind
    if type_key == STATEMENTS_FOLDER:
        kind = "statement"
    elif type_key == TAX_FOLDER:
        kind = "tax"
    else:
        return PathClass(
            status="unrecognised",
            person_folder=folder,
            message=f'"{type_folder}" is not a folder the app imports from.',
            hint=_LAYOUT_HINT,
        )
    year = _folder_year(parts) if kind == "tax" else None

    key = normalize_name(folder)
    matches = [(uid, name) for uid, name in people if key in person_keys(name)]
    if len(matches) == 1:
        uid, name = matches[0]
        return PathClass(
            status="ok",
            kind=kind,
            person_folder=folder,
            user_id=uid,
            person_name=name,
            folder_year=year,
        )
    if len(matches) > 1:
        return PathClass(
            status="ambiguous_person",
            kind=kind,
            person_folder=folder,
            folder_year=year,
            message=f'The folder "{folder}" matches more than one person.',
            hint="Rename the folder so it matches exactly one person, or rename a person.",
        )
    if key == normalize_name(doc_names.SHARED):
        return PathClass(
            status="shared",
            kind=kind,
            person_folder=folder,
            folder_year=year,
            message="Files in Shared/ belong to no one, and a statement or tax form needs a person.",
            hint="Move the file into a person's folder (<Person>/Statements/ or <Person>/Tax/<year>/).",
        )
    return PathClass(
        status="unknown_person",
        kind=kind,
        person_folder=folder,
        folder_year=year,
        message=f'No person is named "{folder}".',
        hint=f'Create a person named "{folder}" in the app, or rename the folder to an existing person.',
    )


def _folder_year(parts: Sequence[str]) -> int | None:
    """The ``<year>`` directly under ``Tax/`` (``Person/Tax/2024/W-2.pdf``), if it is one."""
    if len(parts) < 4:
        return None
    name = parts[2].strip()
    if _YEAR.fullmatch(name) and _MIN_YEAR <= int(name) <= _MAX_YEAR:
        return int(name)
    return None
