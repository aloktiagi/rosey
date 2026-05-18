"""Filesystem-backed memory tool with size caps.

Subclasses the SDK's BetaLocalFilesystemMemoryTool, which already enforces:
  - paths must start with /memories
  - resolved path must stay under <base_path>/memories
  - symlink-escape detection

Adds: per-file and total-directory size caps so Claude can't grow memory
unboundedly. The base class's resulting layout is <base_path>/memories/, so
passing base_path="." stores everything in ./memories/.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing_extensions import override

from anthropic.lib.tools._beta_builtin_memory_tool import BetaLocalFilesystemMemoryTool
from anthropic.lib.tools._beta_functions import ToolError
from anthropic.types.beta import (
    BetaMemoryTool20250818CreateCommand,
    BetaMemoryTool20250818InsertCommand,
    BetaMemoryTool20250818StrReplaceCommand,
)

from redact import Redactor

MAX_FILE_BYTES = 100 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024


class FileMemoryTool(BetaLocalFilesystemMemoryTool):
    def __init__(self, base_path: str = "."):
        super().__init__(base_path=base_path)

    def _total_bytes(self, exclude: Path | None = None) -> int:
        total = 0
        for p in self.memory_root.rglob("*"):
            if p.is_file() and p != exclude:
                total += p.stat().st_size
        return total

    def _check_size(self, new_size: int, target: Path) -> None:
        if new_size > MAX_FILE_BYTES:
            raise ToolError(
                f"File would exceed {MAX_FILE_BYTES // 1024}KB limit "
                f"({new_size} bytes). Split into multiple files or shorten."
            )
        existing = target.stat().st_size if target.exists() else 0
        if self._total_bytes(exclude=target) + new_size > MAX_TOTAL_BYTES:
            _ = existing  # quiet linter; useful when reasoning about caps
            raise ToolError(
                f"Total memory would exceed {MAX_TOTAL_BYTES // (1024 * 1024)}MB. "
                f"Delete unused files first."
            )

    @override
    def create(self, command: BetaMemoryTool20250818CreateCommand) -> str:
        target = self._validate_path(command.path)
        self._check_size(len(command.file_text.encode("utf-8")), target)
        return super().create(command)

    @override
    def str_replace(self, command: BetaMemoryTool20250818StrReplaceCommand) -> str:
        # Reject empty old_str up front. The SDK's str_replace uses
        # `content.count(old_str)`, which returns len(content)+1 for an
        # empty needle and triggers the "multiple occurrences" branch with
        # a line number for every position in the file — a multi-KB error
        # blob that wastes tokens and confuses the model.
        if command.old_str == "":
            raise ToolError(
                "old_str must not be empty. To insert text, use the `insert` "
                "command; to overwrite a file, delete and recreate it."
            )
        if command.old_str == command.new_str:
            raise ToolError("old_str and new_str are identical — nothing to replace.")
        target = self._validate_path(command.path)
        if target.is_file():
            content = target.read_text(encoding="utf-8")
            new_size = len(content.replace(command.old_str, command.new_str).encode("utf-8"))
            self._check_size(new_size, target)
        return super().str_replace(command)

    @override
    def insert(self, command: BetaMemoryTool20250818InsertCommand) -> str:
        target = self._validate_path(command.path)
        if target.is_file():
            existing = target.stat().st_size
            new_size = existing + len(command.insert_text.encode("utf-8")) + 1
            self._check_size(new_size, target)
        return super().insert(command)


class RedactingMemoryTool:
    """Redacts text crossing the model <-> local memory tool boundary.

    The underlying memory files are stored with placeholders for newly written
    sensitive values. Tool read results are redacted as a backstop for legacy
    files that may already contain raw PII.
    """

    def __init__(self, inner: FileMemoryTool, redactor: Redactor):
        self.inner = inner
        self.redactor = redactor

    def to_dict(self) -> dict:
        return self.inner.to_dict()

    def call(self, command: dict) -> str:
        redacted = self._redact_command(command)
        try:
            result = self.inner.call(redacted)
        except Exception:
            fallback = self._legacy_match_command(redacted)
            if fallback == redacted:
                raise
            result = self.inner.call(fallback)
        return self.redactor.redact(result)

    def _redact_command(self, command: dict) -> dict:
        redacted = copy.deepcopy(command)
        for key in ("file_text", "insert_text", "new_str"):
            if isinstance(redacted.get(key), str):
                redacted[key] = self.redactor.redact(redacted[key])
        return redacted

    def _legacy_match_command(self, command: dict) -> dict:
        """Let str_replace match old raw files after Claude saw redacted text."""
        if command.get("command") != "str_replace":
            return command
        old_str = command.get("old_str")
        if not isinstance(old_str, str):
            return command
        fallback = copy.deepcopy(command)
        fallback["old_str"] = self.redactor.restore(old_str)
        return fallback
