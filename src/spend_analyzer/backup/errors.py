"""Backup/restore errors. Messages are shown to the user verbatim (CLI and web UI), so each says
what is wrong and, where there is one, what to do about it."""

from __future__ import annotations

from spend_analyzer.core.errors import SpendAnalyzerError


class BackupError(SpendAnalyzerError):
    """Base class for every backup/restore failure."""


class BackupFormatError(BackupError):
    """The file is not a backup, is corrupted or was modified (a hash or an authentication tag
    does not match), or has a layout this version does not accept."""


class PassphraseError(BackupError):
    """The backup is encrypted and no passphrase was given, or it is wrong (an AEAD failure
    cannot tell a wrong passphrase from a modified file, so the message names both)."""


class SchemaTooNewError(BackupError):
    """The backup's database schema is newer than this version of the app knows."""


class RestoreTargetError(BackupError):
    """The data directory cannot be replaced: it already holds data (and `--replace` was not
    given), or its database looks like it is open in another process."""


class BackupBusyError(BackupError):
    """A running server could not be quiesced for the restore (requests or jobs in flight)."""
