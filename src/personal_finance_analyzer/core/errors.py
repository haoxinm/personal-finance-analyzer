"""Domain error hierarchy (§3.4).

Every error raised by core/ingest/classify code is a subclass of `PfaError`. Routers
translate these to HTTP responses at the API boundary; library code never catches bare
`Exception`.

`EgressViolation` is special: it signals that normalization failed to redact something that must
never reach an LLM. It must never be caught and swallowed by a broad `except PfaError`
handler — any such handler must re-raise it explicitly.
"""

from __future__ import annotations


class PfaError(Exception):
    """Base class for every domain error raised by this application."""


class NoTextLayerError(PfaError):
    """The PDF has no extractable text layer (scanned/image-only). Maps to
    `statements.status = 'no_text_layer'`."""


class UnsupportedLayoutError(PfaError):
    """No parser could confidently handle this statement's layout. Maps to
    `statements.status = 'unsupported_layout'`."""


class ParserError(PfaError):
    """A parser recognized the layout but failed to parse it, or a required reconciliation
    signal (e.g. the statement period) could not be read. Maps to `statements.status = 'error'`."""


class EgressViolation(PfaError):
    """A payload destined for an LLM contains a `FORBIDDEN` pattern (§3.7). Raised, never
    swallowed: this indicates a bug in normalization, not a recoverable condition."""


class LLMError(PfaError):
    """An LLM provider call failed (network, auth, malformed response, etc.)."""


class ConfigError(PfaError):
    """Configuration is missing, malformed, or internally inconsistent (e.g. a taxonomy key
    referenced by data was removed from `taxonomy.yaml`)."""
