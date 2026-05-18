"""Local reversible PII redaction for model-bound text.

Rosey needs household context to be useful, but the hosted version should not
send raw contact details, account numbers, invite codes, or secrets to model
providers when a local placeholder will do. This module keeps a per-household
token vault on disk, outside /memories, and replaces sensitive spans with
stable placeholders before text reaches Claude/OpenAI.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_FALSE_VALUES = {"", "0", "off", "false", "no"}
TOKEN_RE = re.compile(r"<([A-Z][A-Z0-9_]+_\d+)>")


@dataclass(frozen=True)
class Match:
    kind: str
    value: str
    start: int
    end: int


PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL", re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")),
    ("URL_CREDENTIAL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:@/]+:[^\s@/]+@[^\s]+", re.IGNORECASE)),
    ("API_KEY", re.compile(r"\b(?:sk|rk|xox[baprs]|gh[pousr]|AKIA|ASIA)[A-Za-z0-9_-]{16,}\b")),
    ("CREDIT_CARD", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("PHONE", re.compile(r"(?<!\w)(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}(?!\w)")),
    ("STREET_ADDRESS", re.compile(r"(?<![\w-])\d{1,6}\s+(?:[A-Za-z0-9#.'-]+\s+){1,6}(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Place|Pl|Way|Terrace|Ter|Circle|Cir)\.?(?:\s+(?:Apt|Unit|Suite|Ste|#)\s*[\w-]+)?(?=\W|$)", re.IGNORECASE)),
    ("ROSEY_CODE", re.compile(r"\bROSEY-[A-Z0-9]{4,12}\b", re.IGNORECASE)),
    ("IP_ADDRESS", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
)


def enabled() -> bool:
    val = os.environ.get("ROSEY_REDACTION", "off").strip().lower()
    return val not in _FALSE_VALUES


def vault_path(memory_base: str | None = None) -> Path:
    explicit = os.environ.get("ROSEY_PII_VAULT")
    if explicit:
        return Path(explicit)

    base = memory_base or os.environ.get("MEMORY_ROOT", "./memories")
    p = Path(base)
    data_root = p.parent if p.name == "memories" else p
    return data_root / "pii" / "vault.json"


class Redactor:
    def __init__(self, path: Path):
        self.path = path
        self._by_token: dict[str, str] = {}
        self._by_hash: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        self._load()

    @classmethod
    def for_memory_base(cls, memory_base: str | None = None) -> "Redactor":
        return cls(vault_path(memory_base))

    def redact(self, text: str | None) -> str:
        if not text:
            return text or ""
        matches = list(self._find_matches(text))
        if not matches:
            return text

        parts: list[str] = []
        cursor = 0
        changed = False
        for match in matches:
            if match.start < cursor:
                continue
            token = self._token_for(match.kind, match.value)
            parts.append(text[cursor:match.start])
            parts.append(token)
            cursor = match.end
            changed = True
        parts.append(text[cursor:])
        if changed:
            self._save()
        return "".join(parts)

    def restore(self, text: str | None) -> str:
        if not text:
            return text or ""

        def repl(m: re.Match[str]) -> str:
            return self._by_token.get(m.group(0), m.group(0))

        return TOKEN_RE.sub(repl, text)

    def _find_matches(self, text: str) -> Iterable[Match]:
        found: list[Match] = []
        for kind, pattern in PATTERNS:
            for m in pattern.finditer(text):
                value = m.group(0)
                if kind == "CREDIT_CARD" and not _valid_cardish(value):
                    continue
                if kind == "IP_ADDRESS" and not _valid_ipv4(value):
                    continue
                found.append(Match(kind, value, m.start(), m.end()))

        # Prefer longer spans when patterns overlap, then keep non-overlapping.
        found.sort(key=lambda m: (m.start, -(m.end - m.start)))
        accepted: list[Match] = []
        occupied_until = -1
        for match in found:
            if match.start >= occupied_until:
                accepted.append(match)
                occupied_until = match.end
        return accepted

    def _token_for(self, kind: str, value: str) -> str:
        digest = _hash_value(kind, value)
        existing = self._by_hash.get(digest)
        if existing:
            return existing

        next_id = self._counters.get(kind, 0) + 1
        token = f"<{kind}_{next_id}>"
        while token in self._by_token:
            next_id += 1
            token = f"<{kind}_{next_id}>"
        self._counters[kind] = next_id
        self._by_hash[digest] = token
        self._by_token[token] = value
        return token

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        self._by_token = {
            str(k): str(v)
            for k, v in (data.get("tokens") or {}).items()
            if TOKEN_RE.fullmatch(str(k))
        }
        self._by_hash = {
            _hash_value(_kind_from_token(token), value): token
            for token, value in self._by_token.items()
        }
        self._counters = {}
        for token in self._by_token:
            kind = _kind_from_token(token)
            idx = int(token.rsplit("_", 1)[1].rstrip(">"))
            self._counters[kind] = max(self._counters.get(kind, 0), idx)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"tokens": dict(sorted(self._by_token.items()))}
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp_name, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


def _hash_value(kind: str, value: str) -> str:
    return hashlib.sha256(f"{kind}\0{value}".encode("utf-8")).hexdigest()


def _kind_from_token(token: str) -> str:
    return token.strip("<>").rsplit("_", 1)[0]


def _valid_ipv4(value: str) -> bool:
    try:
        return all(0 <= int(part) <= 255 for part in value.split("."))
    except ValueError:
        return False


def _valid_cardish(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if len(digits) < 13 or len(digits) > 19:
        return False
    if len(set(digits)) == 1:
        return False
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        n = int(ch)
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0
