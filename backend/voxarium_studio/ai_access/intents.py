"""Private, explicit recovery of requester mutations; never an offline queue."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from . import protocol as p
from .identity import AiAccessConflict, AiAccessError, JsonStore, replace_model

TTL_MS = 24 * 60 * 60 * 1000
MAX_INTENTS = 256


class Intent(p.Strict):
    registration_id: p.Uuid
    request: p.RequestBody
    digest: p.Sha256
    saved_at: p.Timestamp
    response: p.ResponseBody | None = None

    def view(self) -> dict[str, Any]:
        return {
            "registration_id": self.registration_id,
            "command_id": self.request.command_id,
            "op": str(self.request.op),
            "target": self.request.target.model_dump(mode="json"),
            "saved_at": self.saved_at,
            "expires_at": self.saved_at + TTL_MS,
            "state": (str(self.response.state) if self.response and self.response.state
                      else "outcome_unknown"),
            "response": self.response.model_dump(mode="json", exclude_none=True)
            if self.response else None,
        }


class _Intents(p.Strict):
    entries: list[Intent] = Field(default_factory=list, max_length=MAX_INTENTS)


class IntentStore:
    def __init__(self) -> None:
        self._store = JsonStore("ai-access-intents.json")

    def _load(self, now: int) -> list[Intent]:
        raw = self._store.read()
        if raw is None:
            return []
        try:
            entries = _Intents.from_wire(raw).entries
        except p.ProtocolError:
            raise AiAccessError("ai_access_store_unavailable") from None
        kept = [entry for entry in entries if entry.saved_at <= now < entry.saved_at + TTL_MS]
        if len(entries) != len(kept):
            self._save(kept)
        return kept

    def _save(self, entries: list[Intent]) -> None:
        self._store.write(_Intents(entries=entries).model_dump(mode="json"))

    def stage(self, registration_id: str, request: p.RequestBody, now: int) -> Intent:
        if request.op not in p.MUTATIONS:
            raise p.ProtocolError()
        request.parsed_payload()
        digest = p.canonical_hash(request.model_dump(mode="json"))
        with self._store.mutation():
            entries = self._load(now)
            for entry in entries:
                if entry.request.command_id == request.command_id:
                    if entry.registration_id != registration_id or entry.digest != digest:
                        raise AiAccessConflict(str(p.ErrorCode.IDEMPOTENCY_CONFLICT))
                    raise AiAccessConflict("recover_explicitly")
            if len(entries) >= MAX_INTENTS:
                raise AiAccessError(str(p.ErrorCode.CAPACITY_EXCEEDED))
            entry = Intent(registration_id=registration_id, request=request, digest=digest, saved_at=now)
            self._save([*entries, entry])
            return entry

    def get(self, command_id: str, now: int) -> Intent:
        with self._store.mutation():
            for entry in self._load(now):
                if entry.request.command_id == command_id:
                    return entry
        raise AiAccessError(str(p.ErrorCode.NOT_AVAILABLE))

    def list(self, now: int) -> list[dict[str, Any]]:
        with self._store.mutation():
            return [entry.view() for entry in self._load(now)]

    def discard(self, command_id: str, now: int) -> None:
        with self._store.mutation():
            self._save([entry for entry in self._load(now) if entry.request.command_id != command_id])

    def record(self, command_id: str, response: p.ResponseBody, now: int) -> None:
        with self._store.mutation():
            entries = self._load(now)
            for i, entry in enumerate(entries):
                if entry.request.command_id == command_id and entry.request.target == response.target:
                    entries[i] = replace_model(entry, response=response)
                    self._save(entries)
                    return
