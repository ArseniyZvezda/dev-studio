"""ACP/1 authentication and authorisation on the Remote (ai-access design §4–§6, §9; R-A1).

Two levels, both checked on *every* request, response and stream delivery — not only at
upgrade:

1. the personal Remote key, through the existing :class:`~.auth.Authenticator` and the
   user's credential generation (R-A0: rotate/revoke/reactivate change it);
2. the device registration: ``X-VXS-Client-ID`` + ``X-VXS-Device-Token`` matched against a
   hash-only secret, bound to the key generation it was registered under.

Then a grant mirror decides what the device may do on one exact target. The Documents
model ("every active key sees every space") is not extended to AI: no grant, no access.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Any, Mapping

from . import ai_protocol as p
from .ai_store import AiStore, Export, Grant, Registration
from .auth import Authenticator, User, credentials_from_headers
from .store import Store

DEVICE_TOKEN_PREFIX = "vxd_"
CLIENT_HEADER = "x-vxs-client-id"
DEVICE_HEADER = "x-vxs-device-token"


def new_device_token() -> str:
    """A random 256-bit device secret, returned once to the registering client."""
    return DEVICE_TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_device_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Actor:
    """The authenticated principal of a control socket. Never taken from JSON."""

    user_id: str
    credential_version: int
    registration_id: str
    client_id: str
    kind: str

    @property
    def fingerprint(self) -> tuple[str, int, str]:
        return (self.user_id, self.credential_version, self.registration_id)

    def source(self) -> dict:
        return {"user_id": self.user_id, "registration_id": self.registration_id}


@dataclass(frozen=True, slots=True)
class Authorization:
    grant: Grant
    export: Export
    resource: dict | None
    created_by_grantee: bool


def _single(headers: Any, name: str) -> str | None:
    """Exactly one occurrence, or None (duplicate headers are refused, §6)."""
    if hasattr(headers, "getlist"):
        values = headers.getlist(name)
        if len(values) != 1:
            return None
        return values[0]
    return headers.get(name)


def has_device_headers(headers: Any) -> bool:
    if hasattr(headers, "getlist"):
        return bool(headers.getlist(CLIENT_HEADER) or headers.getlist(DEVICE_HEADER))
    return CLIENT_HEADER in headers or DEVICE_HEADER in headers


class AiAuth:
    def __init__(self, store: Store, ai_store: AiStore, authenticator: Authenticator) -> None:
        self.store, self.ai, self._auth = store, ai_store, authenticator

    # ------------------------------------------------------------- level 1: personal key
    def personal(self, headers: Mapping[str, str] | Any) -> User | None:
        header = _single(headers, "authorization")
        if header is None:
            return None
        credentials = credentials_from_headers({"authorization": header})
        if credentials is None or credentials.scheme != "bearer":
            return None
        user = self._auth.authenticate(credentials)
        if user is None or not self.store.credential_valid(user.id, user.credential_version):
            return None
        return user

    # ------------------------------------------------------------- level 2: device
    def device(self, headers: Mapping[str, str] | Any) -> Actor | None:
        user = self.personal(headers)
        if user is None:
            return None
        client_id, token = _single(headers, CLIENT_HEADER), _single(headers, DEVICE_HEADER)
        if not client_id or not token or len(token) > 256:
            return None
        try:
            p.check_uuid4(client_id)
        except p.AcpError:
            return None
        registration = self.ai.registration_by_client(user.id, client_id)
        digest = hash_device_token(token)
        if (registration is None or registration.token_hash is None
                # Constant-time compare of digests; the lookup was by (user, client_id).
                or not hmac.compare_digest(registration.token_hash, digest)
                or registration.user_credential_version != user.credential_version):
            return None
        return Actor(user.id, user.credential_version, registration.id, registration.client_id,
                     registration.kind)

    def registration_live(self, registration: Registration | None) -> bool:
        return (registration is not None and registration.active and
                self.store.credential_valid(registration.user_id, registration.user_credential_version))

    def current(self, actor: Actor) -> bool:
        """Per-frame guard: both credentials still the ones the socket authenticated with."""
        if not self.store.credential_valid(actor.user_id, actor.credential_version):
            return False
        registration = self.ai.registration(actor.registration_id)
        return (registration is not None and registration.active
                and registration.user_id == actor.user_id
                and registration.user_credential_version == actor.credential_version)

    # ------------------------------------------------------------- grants
    def live_export(self, export_id: str) -> Export | None:
        export = self.ai.export(export_id)
        if export is None or export.state != "published":
            return None
        if not self.registration_live(self.ai.registration(export.executor_registration_id)):
            return None
        return export

    def target_of(self, export: Export) -> dict:
        return {"server_instance_id": self.ai.server_instance_id,
                "executor_registration_id": export.executor_registration_id,
                "export_id": export.export_id, "persistent_id": export.persistent_id,
                "storage_epoch": export.storage_epoch, "export_revision": export.revision}

    def grant_effective(self, grant: Grant | None, actor: Actor, now_ms: int) -> bool:
        return (grant is not None and grant.state == "active" and grant.record is not None
                and grant.grantee_user_id == actor.user_id
                and grant.grantee_registration_id == actor.registration_id
                and now_ms < grant.expires_at)

    def authorize(self, actor: Actor, body: dict, now_ms: int) -> Authorization:
        """Server-side check of one ``request`` (design §6). Raises :class:`RequestRejected`.

        Unknown or foreign objects all answer ``not_available``: no existence oracle.
        """
        target = body["target"]
        if target["server_instance_id"] != self.ai.server_instance_id:
            raise p.RequestRejected("stale_target")
        grant = self.ai.grant(body["grant_id"])
        if (grant is None or grant.export_id != target["export_id"]
                or grant.grantee_user_id != actor.user_id
                or grant.grantee_registration_id != actor.registration_id):
            raise p.RequestRejected("not_available")
        if grant.state != "active" or grant.record is None:
            raise p.RequestRejected("denied")
        if now_ms >= grant.expires_at:
            raise p.RequestRejected("expired")
        export = self.live_export(target["export_id"])
        if export is None:
            raise p.RequestRejected("not_available")
        if (self.target_of(export) != target or grant.revision != body["grant_revision"]
                or grant.export_revision != export.revision):
            raise p.RequestRejected("stale_target")
        record = grant.record
        op, payload = body["op"], body["payload"]
        required, mutation, needs_resource = p.OPERATIONS[op]
        required = set(required)
        if op == "stream.subscribe" and payload["topic"] == "transcript":
            required.add("view_transcript")
        operations = set(record["operations"])
        if not required <= operations:
            raise p.RequestRejected("denied")
        if mutation and body["issued_at"] > now_ms + p.MAX_ISSUED_SKEW_MS:
            # Age/epoch of an *unknown* command is the PC's ledger decision (§7.0); a
            # timestamp from the future is never acceptable.
            raise p.RequestRejected("expired")
        teams, members = set(record["team_ids"]), set(record["member_ids"])
        if op in ("task.create", "chat.create"):
            if payload["team_id"] not in teams or payload["member_id"] not in members:
                raise p.RequestRejected("denied")
        if op == "resource.list" and "team_id" in payload and payload["team_id"] not in teams:
            raise p.RequestRejected("not_available")
        resource, own = None, False
        if needs_resource:
            resource = self.ai.index_row(export.export_id, export.revision, body["resource_id"])
            if resource is None or resource["team_id"] not in teams:
                raise p.RequestRejected("not_available")
            if members and resource["member_id"] is not None and resource["member_id"] not in members:
                raise p.RequestRejected("not_available")
            own = resource["remote_origin"] == actor.source()
            scope = record["resource_scope"]
            if ((scope == "created_by_this_grantee" and not own) or
                    (scope == "selected_resources" and resource["resource_id"] not in record["resource_ids"])):
                raise p.RequestRejected("not_available")
            if mutation and not own and "operate_existing" not in operations:
                raise p.RequestRejected("denied")
        return Authorization(grant, export, resource, own)

    def visible_grants(self, actor: Actor, now_ms: int) -> list[tuple[Grant, Export]]:
        result = []
        for grant in self.ai.grants_for(actor.registration_id):
            if not self.grant_effective(grant, actor, now_ms):
                continue
            export = self.live_export(grant.export_id)
            if export is not None and export.revision == grant.export_revision:
                result.append((grant, export))
        return result
