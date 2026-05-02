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

from pathlib import Path
from typing_extensions import override

from anthropic.lib.tools._beta_builtin_memory_tool import BetaLocalFilesystemMemoryTool
from anthropic.lib.tools._beta_functions import ToolError
from anthropic.types.beta import (
    BetaMemoryTool20250818CreateCommand,
    BetaMemoryTool20250818InsertCommand,
    BetaMemoryTool20250818StrReplaceCommand,
)

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
