"""Typed errors.

Every failure mode that the executor or the CLI has to *report honestly*
gets its own type, so callers never have to string-match messages.
"""

from __future__ import annotations


class SeerAutoError(Exception):
    """Base class for every seerauto error."""


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
class ConfigError(SeerAutoError):
    """Config file missing, malformed, or referring to unknown keys."""


# --------------------------------------------------------------------------- #
# window / capture
# --------------------------------------------------------------------------- #
class WindowNotFound(SeerAutoError):
    """The game window is not on screen (or cannot be enumerated)."""


class WindowMoved(SeerAutoError):
    """Window geometry changed beyond tolerance; refusing to send input."""


class CaptureError(SeerAutoError):
    """A capture backend failed."""


class CaptureUnavailable(CaptureError):
    """Backend not available on this host (binary missing, op unsupported)."""


class CapturePermissionDenied(CaptureError):
    """Screen-recording permission denied."""


class DaemonError(CaptureError):
    """The JSON-line daemon replied with an error or died."""


# --------------------------------------------------------------------------- #
# control flow
# --------------------------------------------------------------------------- #
class BudgetExceeded(SeerAutoError):
    """Loop budget (iterations / wall clock) exhausted."""


class StateTimeout(SeerAutoError):
    """Waiting for a state change timed out."""


class SafetyViolation(SeerAutoError):
    """An action was refused because a safety precondition failed."""
