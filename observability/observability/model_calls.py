"""Prompt/response inspector: capture what the model was actually asked.

Blueprint ref: section 04.4 - the observability layer's job is to answer "why did
it do that?". Token counts and latency answer *how much* and *how long*; they do
not answer what the model was told, which is the question an operator asks when a
plan comes back wrong.

Two properties make this safe to ship, and both are the reason it is a module
rather than a log line
-------------------------------------------------------------------------------
1. **Redaction before storage.** A prompt in this system contains tool output and
   can contain an API key, a bearer token or a credential passed on a command
   line. Capturing that verbatim would turn the observability store into the most
   sensitive database on the box - and a second copy of every secret, outside
   the audit trail. Secrets are replaced with a marker *and counted*, so the
   operator can see that something was removed rather than silently reading a
   prompt that looks complete.
2. **Truncation, declared.** An unbounded prompt capture grows without limit and
   a huge one is unreadable anyway. Fields are cut at ``max_chars`` and the
   record says ``truncated: true`` with the original length. A truncated prompt
   that does not admit it is worse than no capture: it invites a conclusion drawn
   from a fragment.

The store is deliberately a bounded in-memory ring. The full prompt is diagnostic,
not evidentiary - the *evidence* is the hash-chained audit log, which records the
decision and its hash. Keeping the two separate is what stops a diagnostic buffer
from being mistaken for a tamper-evident record.
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

#: Default per-field capture limit, in characters.
DEFAULT_MAX_CHARS = 8000

#: Redaction rules, applied before a field is stored.
#:
#: Ordered most-specific first: a ``Bearer`` token must be matched before the
#: generic ``token=`` rule, or the header rule would leave the value behind.
_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "bearer",
        re.compile(r"(?i)\b(authorization\s*:\s*bearer)\s+[A-Za-z0-9._\-]{8,}"),
        r"\1 <redacted:bearer>",
    ),
    (
        "basic",
        re.compile(r"(?i)\b(authorization\s*:\s*basic)\s+[A-Za-z0-9+/=]{8,}"),
        r"\1 <redacted:basic>",
    ),
    (
        "api_key",
        re.compile(r"(?i)\b(api[_-]?key|apikey|x-api-key)\b\s*[=:]\s*[\"']?([A-Za-z0-9._\-]{12,})"),
        r"\1=<redacted:api_key>",
    ),
    (
        "token",
        re.compile(r"(?i)\b(access[_-]?token|auth[_-]?token|token)\b\s*[=:]\s*[\"']?([A-Za-z0-9._\-]{12,})"),
        r"\1=<redacted:token>",
    ),
    (
        "secret",
        re.compile(r"(?i)\b(client[_-]?secret|secret)\b\s*[=:]\s*[\"']?([A-Za-z0-9._\-]{8,})"),
        r"\1=<redacted:secret>",
    ),
    (
        "password",
        re.compile(r"(?i)\b(pass(word|wd)?|pwd)\b\s*[=:]\s*[\"']?([^\s\"']{4,})"),
        r"\1=<redacted:password>",
    ),
    (
        # ``-p`` is a password flag for a large family of tools, which is exactly
        # how a credential ends up inside a captured prompt.
        #
        # The value must look like a *secret*, not like a port list: an unbounded
        # rule here redacts ``nmap -p 22,80,443``, and a redactor that mangles
        # ordinary command output is one an operator switches off - which loses the
        # real redactions too. A comma-separated list and a bare number are
        # therefore explicitly excluded.
        "flag_password",
        re.compile(
            r"(?<=\s)-p\s*((?![0-9,\s]+(?=\s|$))[^\s\-][^\s]{3,})"
        ),
        " -p <redacted:password>",
    ),
    (
        # Cloud static keys are the most damaging credential to leave in a
        # captured prompt, and they arrive **without a key/value marker** - a
        # bare ``AKIA...`` read out of ``~/.aws/credentials``, an env dump or a
        # tool's own output. Every rule above needs a ``name=`` or a known
        # header to match, so none of them fires on a naked key. AWS's own
        # documented prefixes are unambiguous and fixed-length, which is what
        # makes a bare-token rule safe here: it cannot swallow ordinary prose.
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASCA)[A-Z0-9]{16}\b"),
        "<redacted:aws_access_key>",
    ),
)


def redact(text: str) -> tuple[str, list[str]]:
    """Replace credential-shaped substrings. Returns ``(text, kinds_hit)``.

    Returns the *kinds* rather than a count alone: "1 secret removed" is not
    actionable, while "bearer, api_key" tells the operator which credential a
    prompt was carrying and therefore what may need rotating.
    """
    if not text:
        return text, []
    hits: list[str] = []
    out = text
    for name, pattern, replacement in _RULES:
        out, count = pattern.subn(replacement, out)
        if count:
            hits.append(name)
    return out, hits


@dataclass
class CapturedCall:
    """One model interaction, as the inspector shows it."""

    id: str
    ts: float
    model: str = ""
    backend: str = ""
    card_id: Optional[str] = None
    crew: Optional[str] = None
    role: Optional[str] = None
    ok: bool = True
    error: Optional[str] = None
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    prompt: str = ""
    response: str = ""
    prompt_chars: int = 0
    response_chars: int = 0
    truncated: bool = False
    redactions: list[str] = field(default_factory=list)
    parsed: Optional[dict[str, Any]] = None

    def as_dict(self, *, full: bool = False) -> dict[str, Any]:
        """Serialize. ``full=False`` gives a list row without the bodies.

        The list endpoint omits prompt/response on purpose: a board view that
        fetches every prompt body would put a megabyte of tool output through
        the dashboard on every poll, and the operator only opens one call at a
        time.
        """
        out: dict[str, Any] = {
            "id": self.id,
            "ts": self.ts,
            "model": self.model,
            "backend": self.backend,
            "card_id": self.card_id,
            "crew": self.crew,
            "role": self.role,
            "ok": self.ok,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "prompt_chars": self.prompt_chars,
            "response_chars": self.response_chars,
            "truncated": self.truncated,
            "redactions": list(self.redactions),
        }
        if full:
            out["prompt"] = self.prompt
            out["response"] = self.response
            out["parsed"] = self.parsed
        return out


class ModelCallStore:
    """Bounded ring of captured model calls, with redaction and truncation."""

    def __init__(
        self,
        *,
        max_records: int = 500,
        max_chars: int = DEFAULT_MAX_CHARS,
        clock: Any = time.time,
    ) -> None:
        self.max_records = max_records
        self.max_chars = max_chars
        self._clock = clock
        self._records: deque[CapturedCall] = deque(maxlen=max_records)
        self._seq = 0
        self._lock = threading.RLock()
        self.captured = 0
        self.redacted_fields = 0
        self.truncated_fields = 0

    # ------------------------------------------------------------- capture
    def record(
        self,
        *,
        prompt: str = "",
        response: str = "",
        model: str = "",
        backend: str = "",
        card_id: Optional[str] = None,
        crew: Optional[str] = None,
        role: Optional[str] = None,
        ok: bool = True,
        error: Optional[str] = None,
        latency_ms: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        cost_usd: float = 0.0,
        parsed: Optional[dict[str, Any]] = None,
    ) -> CapturedCall:
        with self._lock:
            self._seq += 1
            call_id = f"mc_{self._seq:05d}"

            clean_prompt, prompt_hits = redact(prompt or "")
            clean_response, response_hits = redact(response or "")
            hits = sorted(set(prompt_hits) | set(response_hits))

            prompt_chars = len(prompt or "")
            response_chars = len(response or "")
            truncated = prompt_chars > self.max_chars or response_chars > self.max_chars
            if truncated:
                self.truncated_fields += 1
            if hits:
                self.redacted_fields += 1

            record = CapturedCall(
                id=call_id,
                ts=self._clock(),
                model=model,
                backend=backend,
                card_id=card_id,
                crew=crew,
                role=role,
                ok=ok,
                error=error,
                latency_ms=int(latency_ms or 0),
                prompt_tokens=int(prompt_tokens or 0),
                completion_tokens=int(completion_tokens or 0),
                total_tokens=int(total_tokens or 0),
                cost_usd=float(cost_usd or 0.0),
                prompt=clean_prompt[: self.max_chars],
                response=clean_response[: self.max_chars],
                prompt_chars=prompt_chars,
                response_chars=response_chars,
                truncated=truncated,
                redactions=hits,
                parsed=parsed,
            )
            self._records.append(record)
            self.captured += 1
            return record

    def capture_from_response(
        self,
        *,
        prompt: str,
        response: Any,
        card_id: Optional[str] = None,
        crew: Optional[str] = None,
        role: Optional[str] = None,
    ) -> CapturedCall:
        """Capture straight from a :class:`ModelResponse`-shaped object.

        Duck-typed rather than importing ``agent_runtime``: the observability
        service must not depend on the runtime package, or the two services stop
        being independently deployable.
        """
        return self.record(
            prompt=prompt,
            response=getattr(response, "text", "") or "",
            model=getattr(response, "model", "") or "",
            backend=getattr(response, "backend", "") or "",
            card_id=card_id,
            crew=crew,
            role=role,
            ok=bool(getattr(response, "ok", True)),
            error=getattr(response, "error", None),
            latency_ms=int(getattr(response, "latency_ms", 0) or 0),
            prompt_tokens=int(getattr(response, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(response, "completion_tokens", 0) or 0),
            total_tokens=int(getattr(response, "total_tokens", 0) or 0),
            cost_usd=float(getattr(response, "cost_usd", 0.0) or 0.0),
            parsed=getattr(response, "parsed", None),
        )

    # -------------------------------------------------------------- reads
    def list(
        self,
        *,
        limit: int = 50,
        card_id: Optional[str] = None,
        ok: Optional[bool] = None,
        redacted_only: bool = False,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self._lock:
            for record in reversed(self._records):
                if card_id and record.card_id != card_id:
                    continue
                if ok is not None and record.ok != ok:
                    continue
                if redacted_only and not record.redactions:
                    continue
                out.append(record.as_dict())
                if len(out) >= limit:
                    break
        return out

    def get(self, call_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            for record in self._records:
                if record.id == call_id:
                    return record.as_dict(full=True)
        return None

    def counts(self) -> dict[str, Any]:
        with self._lock:
            failed = sum(1 for r in self._records if not r.ok)
            tokens = sum(r.total_tokens for r in self._records)
            latency = sum(r.latency_ms for r in self._records)
            return {
                "captured": self.captured,
                "buffered": len(self._records),
                "max_records": self.max_records,
                "max_chars": self.max_chars,
                "failed": failed,
                "tokens": tokens,
                "avg_latency_ms": int(latency / len(self._records)) if self._records else 0,
                "redacted_calls": sum(1 for r in self._records if r.redactions),
                "redaction_kinds": sorted({k for r in self._records for k in r.redactions}),
                "truncated_calls": sum(1 for r in self._records if r.truncated),
            }

    def clear(self) -> int:
        with self._lock:
            removed = len(self._records)
            self._records.clear()
            return removed
