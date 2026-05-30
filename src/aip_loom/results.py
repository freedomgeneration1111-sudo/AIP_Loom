"""Shared command-result envelope for AIP_Loom.

Every CLI command must return a ``CommandResult``.  This is the single
response shape — there are no command-specific envelope variants.

The envelope is frozen (immutable after construction) so that downstream
code cannot silently mutate results.

Serialization helpers (``to_dict``, ``to_json``) produce the canonical
wire format consumed by ``--json`` output and by tests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .errors import LoomError, LoomWarning


@dataclass(frozen=True)
class CommandResult:
    """Universal response envelope for every ``aip-loom`` command.

    Attributes
    ----------
    ok:
        Whether the command succeeded.  ``True`` only when the command
        fully completed its postconditions.
    command:
        The CLI command name (e.g. ``"init"``, ``"status"``).
    code:
        Stable result code.  On success this is ``"OK"``; on failure it
        must be a code from :mod:`aip_loom.errors`.
    message:
        Human-readable summary of the outcome.
    data:
        Arbitrary machine-readable payload.  Empty on failure unless the
        failure contract explicitly provides diagnostic data.
    warnings:
        Non-fatal warnings accumulated during execution.
    errors:
        Fatal errors that caused the command to fail.  Empty when ``ok``
        is ``True``.
    """

    ok: bool
    command: str
    code: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    warnings: list[LoomWarning] = field(default_factory=list)
    errors: list[LoomError] = field(default_factory=list)

    # -- convenience constructors -------------------------------------------

    @classmethod
    def success(
        cls,
        command: str,
        message: str = "",
        data: dict[str, Any] | None = None,
        warnings: list[LoomWarning] | None = None,
    ) -> CommandResult:
        """Create a successful result envelope."""
        return cls(
            ok=True,
            command=command,
            code="OK",
            message=message,
            data=data if data is not None else {},
            warnings=warnings if warnings is not None else [],
            errors=[],
        )

    @classmethod
    def failure(
        cls,
        command: str,
        code: str,
        message: str,
        errors: list[LoomError] | None = None,
        data: dict[str, Any] | None = None,
        warnings: list[LoomWarning] | None = None,
    ) -> CommandResult:
        """Create a failure result envelope.

        ``ok`` is always ``False``.  At least one :class:`LoomError` must be
        provided or a single error will be synthesised from *code* and
        *message*.
        """
        if errors is None:
            errors = [LoomError(code=code, message=message)]
        return cls(
            ok=False,
            command=command,
            code=code,
            message=message,
            data=data if data is not None else {},
            warnings=warnings if warnings is not None else [],
            errors=errors,
        )

    # -- serialization ------------------------------------------------------

    @staticmethod
    def _sanitize_data(data: dict[str, Any]) -> dict[str, Any]:
        """Strip internal (non-serializable) keys from data.

        Keys prefixed with ``_`` are considered internal references
        (e.g. ``_parsed_block``) that should not appear in serialized
        output.  They hold live Python objects for downstream code to
        use directly, but are not JSON-serializable.
        """
        return {k: v for k, v in data.items() if not k.startswith("_")}

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary of the envelope."""
        return {
            "ok": self.ok,
            "command": self.command,
            "code": self.code,
            "message": self.message,
            "data": self._sanitize_data(self.data),
            "warnings": [
                {"code": w.code, "message": w.message, "detail": w.detail}
                for w in self.warnings
            ],
            "errors": [
                {"code": e.code, "message": e.message, "detail": e.detail}
                for e in self.errors
            ],
        }

    def to_json(self) -> str:
        """Return a JSON string of the envelope."""
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    # -- exit code ----------------------------------------------------------

    @property
    def exit_code(self) -> int:
        """Return the process exit code: 0 for success, 1 for failure."""
        return 0 if self.ok else 1
