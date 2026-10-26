"""현재 서버 세션에 고정된 비직렬화 request capability."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Generic, Literal, Protocol, TypeVar

from app.ticketing.models import SafeErrorV1
from app.ticketing.update_models import UpdateCredentialRefreshReservationV1


@dataclass(frozen=True)
class DelegatedCredential:
    """Raw 값은 capability와 gateway 사이에서만 잠깐 사용한다."""

    token: str = field(repr=False)
    expires_at: datetime | None = None
    generation: int = 0


CredentialProvider = Callable[[], Awaitable[DelegatedCredential]]
ExpiryHandler = Callable[[], Awaitable[None]]
UtcClock = Callable[[], datetime]
MonotonicClock = Callable[[], float]
SuccessT = TypeVar("SuccessT")
ErrorT = TypeVar("ErrorT")

_REFRESH_KID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class CapabilityAuthenticationError(Exception):
    pass


@dataclass(frozen=True)
class Result(Generic[SuccessT, ErrorT]):
    """Small typed port result shared by the C-01 provider and consumer."""

    value: SuccessT | None = None
    error: ErrorT | None = None

    def __post_init__(self) -> None:
        if (self.value is None) == (self.error is None):
            raise ValueError("result must contain exactly one of value or error")

    @property
    def ok(self) -> bool:
        return self.error is None

    @classmethod
    def success(cls, value: SuccessT) -> Result[SuccessT, ErrorT]:
        return cls(value=value)

    @classmethod
    def failure(cls, error: ErrorT) -> Result[SuccessT, ErrorT]:
        return cls(error=error)


def valid_refresh_key_id(value: str) -> bool:
    return bool(_REFRESH_KID.fullmatch(value))


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _utc_text(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("timezone-aware refresh time required")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class CredentialRefreshReservationV1:
    """Server-only durable U2 claim passed to the U1 identity boundary."""

    create_attempt_id: str
    refresh_reservation_id: str = field(repr=False)
    refresh_used_at: datetime
    base_credential_generation: int | None = None
    refresh_state: Literal["reserved", "committed", "failed"] | None = None
    committed_credential_generation: int | None = None
    refresh_committed_at: datetime | None = None


@dataclass(frozen=True)
class _VerifiedRefreshClaims:
    base_credential_generation: int
    creator_boot_id: str
    operation_deadline: datetime


class ActiveCredentialRefreshReservations:
    """Process-local owner registry; durable reservation state remains in SQLite."""

    def __init__(self) -> None:
        self._reservation_ids: set[str] = set()

    def register(self, reservation_id: str) -> bool:
        if not reservation_id or reservation_id in self._reservation_ids:
            return False
        self._reservation_ids.add(reservation_id)
        return True

    def discard(self, reservation_id: str) -> None:
        self._reservation_ids.discard(reservation_id)

    def contains(self, reservation_id: str) -> bool:
        return reservation_id in self._reservation_ids


def _actor_binding(secret: bytes, domain: str, value: object) -> str:
    message = f"ticket-tools-refresh-v1:{domain}:".encode("ascii") + str(value).encode("utf-8")
    return _b64encode(hmac.new(secret, message, hashlib.sha256).digest())


class CredentialRefreshReservationSigner:
    """U2-only signing authority; exposes no verification or raw key accessor."""

    def __init__(self, *, kid: str, secret: bytes, creator_boot_id: str) -> None:
        if not valid_refresh_key_id(kid) or len(secret) < 32 or not creator_boot_id:
            raise ValueError("valid refresh signing key and boot id are required")
        self._kid = kid
        self._secret = secret
        self._creator_boot_id = creator_boot_id

    def mint(
        self,
        capability: TicketRequestCapability,
        *,
        create_attempt_id: str,
        refresh_used_at: datetime,
        operation_deadline: datetime,
        nonce: str | None = None,
    ) -> CredentialRefreshReservationV1:
        if not create_attempt_id:
            raise ValueError("create attempt id is required")
        body = {
            "create_attempt_id": create_attempt_id,
            "user_binding": _actor_binding(self._secret, "user", capability.user_id),
            "session_binding": _actor_binding(self._secret, "session", capability.session_id),
            "conversation_binding": _actor_binding(
                self._secret, "conversation", capability.conversation_id
            ),
            "refresh_used_at": _utc_text(refresh_used_at),
            "base_credential_generation": capability.credential_generation,
            "creator_boot_id": self._creator_boot_id,
            "operation_deadline_utc": _utc_text(operation_deadline),
            "nonce": nonce or secrets.token_urlsafe(16),
        }
        encoded = _b64encode(json.dumps(body, separators=(",", ":")).encode("utf-8"))
        mac_input = b"ticket-tools-refresh-v1" + self._kid.encode("ascii") + encoded.encode("ascii")
        signature = _b64encode(hmac.new(self._secret, mac_input, hashlib.sha256).digest())
        return CredentialRefreshReservationV1(
            create_attempt_id=create_attempt_id,
            refresh_reservation_id=f"r1.{self._kid}.{encoded}.{signature}",
            refresh_used_at=refresh_used_at,
            base_credential_generation=capability.credential_generation,
            refresh_state="reserved",
        )


class CredentialRefreshReservationVerifier:
    """Authenticate opaque reservation envelopes without reading U2 storage."""

    def __init__(
        self,
        keys: Mapping[str, bytes],
        *,
        current_kid: str,
        current_boot_id: str,
        utc_now: UtcClock = lambda: datetime.now(timezone.utc),
        max_age: timedelta = timedelta(minutes=15),
    ) -> None:
        if not valid_refresh_key_id(current_kid) or current_kid not in keys:
            raise ValueError("current reservation key id is required")
        if not current_boot_id:
            raise ValueError("current boot id is required")
        if any(not valid_refresh_key_id(kid) or len(secret) < 32 for kid, secret in keys.items()):
            raise ValueError("reservation verifier keys must be named and at least 256 bits")
        self._keys = dict(keys)
        self._current_kid = current_kid
        self._current_boot_id = current_boot_id
        self._utc_now = utc_now
        self._max_age = max_age

    def _verified_claims(
        self,
        capability: TicketRequestCapability,
        reservation: CredentialRefreshReservationV1,
        *,
        require_current_boot: bool,
        allow_generation_advance: bool,
    ) -> _VerifiedRefreshClaims | None:
        try:
            parts = reservation.refresh_reservation_id.split(".")
            if len(parts) != 4:
                return None
            version, kid, encoded, supplied_signature = parts
            if version != "r1" or not valid_refresh_key_id(kid) or kid not in self._keys:
                return None
            secret = self._keys[kid]
            mac_input = b"ticket-tools-refresh-v1" + kid.encode("ascii") + encoded.encode("ascii")
            expected_signature = _b64encode(
                hmac.new(secret, mac_input, hashlib.sha256).digest()
            )
            if not hmac.compare_digest(supplied_signature, expected_signature):
                return None
            body = json.loads(_b64decode(encoded))
            if set(body) != {
                "create_attempt_id",
                "user_binding",
                "session_binding",
                "conversation_binding",
                "refresh_used_at",
                "base_credential_generation",
                "creator_boot_id",
                "operation_deadline_utc",
                "nonce",
            }:
                return None
            used_at = datetime.fromisoformat(str(body["refresh_used_at"]).replace("Z", "+00:00"))
            operation_deadline = datetime.fromisoformat(
                str(body["operation_deadline_utc"]).replace("Z", "+00:00")
            )
            base_generation = int(body["base_credential_generation"])
            creator_boot_id = str(body["creator_boot_id"])
        except (
            binascii.Error,
            ValueError,
            TypeError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ):
            return None

        try:
            now = self._utc_now()
            age = now - used_at
            public_used_at = _utc_text(reservation.refresh_used_at)
        except (TypeError, ValueError):
            return None
        generation_matches = (
            capability.credential_generation >= base_generation
            if allow_generation_advance
            else capability.credential_generation == base_generation
        )
        boot_matches = (
            creator_boot_id == self._current_boot_id
            if require_current_boot
            else creator_boot_id != self._current_boot_id
        )
        valid = all(
            (
                bool(body["nonce"]),
                base_generation >= 0,
                body["create_attempt_id"] == reservation.create_attempt_id,
                secrets.compare_digest(
                    str(body["user_binding"]),
                    _actor_binding(secret, "user", capability.user_id),
                ),
                secrets.compare_digest(
                    str(body["session_binding"]),
                    _actor_binding(secret, "session", capability.session_id),
                ),
                secrets.compare_digest(
                    str(body["conversation_binding"]),
                    _actor_binding(secret, "conversation", capability.conversation_id),
                ),
                public_used_at == body["refresh_used_at"],
                reservation.base_credential_generation in (None, base_generation),
                reservation.refresh_state in (None, "reserved"),
                reservation.committed_credential_generation is None,
                reservation.refresh_committed_at is None,
                generation_matches,
                boot_matches,
                operation_deadline.utcoffset() is not None,
                timedelta(0) <= age <= self._max_age,
            )
        )
        if not valid:
            return None
        return _VerifiedRefreshClaims(base_generation, creator_boot_id, operation_deadline)

    def remaining_budget(
        self,
        capability: TicketRequestCapability,
        reservation: CredentialRefreshReservationV1,
    ) -> float | None:
        claims = self._verified_claims(
            capability,
            reservation,
            require_current_boot=True,
            allow_generation_advance=False,
        )
        if claims is None:
            return None
        remaining = (claims.operation_deadline - self._utc_now()).total_seconds()
        return remaining if remaining > 0 else None

    def verify(
        self,
        capability: TicketRequestCapability,
        reservation: CredentialRefreshReservationV1,
    ) -> bool:
        return self.remaining_budget(capability, reservation) is not None

    def restore(
        self,
        capability: TicketRequestCapability,
        *,
        create_attempt_id: str,
        refresh_reservation_id: str,
        refresh_used_at: datetime,
        base_credential_generation: int,
    ) -> CredentialRefreshReservationV1 | None:
        """Verify a durable reservation from a prior process boot."""
        reservation = CredentialRefreshReservationV1(
            create_attempt_id=create_attempt_id,
            refresh_reservation_id=refresh_reservation_id,
            refresh_used_at=refresh_used_at,
            base_credential_generation=base_credential_generation,
            refresh_state="reserved",
        )
        claims = self._verified_claims(
            capability,
            reservation,
            require_current_boot=False,
            allow_generation_advance=True,
        )
        return reservation if claims is not None else None


@dataclass
class TicketRequestCapability:
    user_id: int
    session_id: str
    conversation_id: str
    _credential_provider: CredentialProvider = field(repr=False)
    _refresh_provider: CredentialProvider = field(repr=False)
    session_expires_at: datetime | None = field(default=None, repr=False)
    _expiry_handler: ExpiryHandler | None = field(default=None, repr=False)
    _utc_now: UtcClock = field(default=lambda: datetime.now(timezone.utc), repr=False)
    _credential: DelegatedCredential | None = field(default=None, init=False, repr=False)
    _credential_generation: int = field(default=0, repr=False)
    _refresh_used: bool = field(default=False, init=False, repr=False)
    _refresh_reservation_id: str | None = field(default=None, init=False, repr=False)

    def operation_scope(self) -> TicketRequestCapability:
        """각 tool operation에 독립된 refresh budget 하나를 제공한다."""
        return TicketRequestCapability(
            user_id=self.user_id,
            session_id=self.session_id,
            conversation_id=self.conversation_id,
            _credential_provider=self._credential_provider,
            _refresh_provider=self._refresh_provider,
            session_expires_at=self.session_expires_at,
            _expiry_handler=self._expiry_handler,
            _utc_now=self._utc_now,
            _credential_generation=self._credential_generation,
        )

    @property
    def refresh_used(self) -> bool:
        return self._refresh_used

    @property
    def credential_generation(self) -> int:
        return self._credential_generation

    async def _delegated_credential_for_gateway(
        self, *, deadline: float, monotonic: MonotonicClock
    ) -> str:
        """Gateway 전용 accessor. refresh 정책 없이 credential만 전달한다."""
        await self._deny_expired_session()
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError
        if self._credential is None:
            self._credential = await asyncio.wait_for(self._credential_provider(), timeout=remaining)
            self._credential_generation = self._credential.generation
        await self._deny_expired_session()
        return self._credential.token

    async def credential_expires_at(
        self, *, deadline: float, monotonic: MonotonicClock
    ) -> datetime | None:
        """Operations preflight에 raw token 없이 expiry만 제공한다."""
        await self._deny_expired_session()
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError
        if self._credential is None:
            self._credential = await asyncio.wait_for(self._credential_provider(), timeout=remaining)
            self._credential_generation = self._credential.generation
        await self._deny_expired_session()
        return self._credential.expires_at

    async def _deny_expired_session(self) -> None:
        if self.session_expires_at is None or self._utc_now() < self.session_expires_at:
            return
        if self._expiry_handler is not None:
            await self._expiry_handler()
        raise CapabilityAuthenticationError("server session expired")

    async def refresh_once(self, *, timeout: float) -> bool:
        """Policy에는 성공 여부만 보이고 새 credential 값은 capability 안에 남는다."""
        return await self._refresh_verified_once("legacy-operation-budget", timeout=timeout)

    async def _refresh_verified_once(self, reservation_id: str, *, timeout: float) -> bool:
        if self._refresh_used or not reservation_id or timeout <= 0:
            return False
        self._refresh_used = True
        self._refresh_reservation_id = reservation_id
        try:
            refreshed = await asyncio.wait_for(self._refresh_provider(), timeout=timeout)
        except Exception:
            self._credential = None
            return False
        if refreshed.generation <= self._credential_generation:
            if reservation_id != "legacy-operation-budget":
                return False
            refreshed = DelegatedCredential(
                refreshed.token,
                refreshed.expires_at,
                self._credential_generation + 1,
            )
        self._credential = refreshed
        self._credential_generation = refreshed.generation
        return True

    def __getstate__(self) -> dict:
        raise TypeError("TicketRequestCapability is not serializable")


class CredentialRefreshPortV1:
    """U1-owned create refresh boundary over an authenticated U2 reservation."""

    def __init__(self, verifier: CredentialRefreshReservationVerifier) -> None:
        self._verifier = verifier

    async def refresh_delegated_capability_once(
        self,
        capability: TicketRequestCapability,
        reservation: CredentialRefreshReservationV1,
        remaining_deadline: float,
    ) -> Result[TicketRequestCapability, SafeErrorV1]:
        remaining_signed_budget = self._verifier.remaining_budget(capability, reservation)
        if (
            remaining_deadline <= 0
            or remaining_signed_budget is None
        ):
            return Result.failure(
                SafeErrorV1(
                    code="authentication_required",
                    message_key="credential_refresh_reservation_invalid",
                    retryable=False,
                )
            )
        refreshed = await capability._refresh_verified_once(
            reservation.refresh_reservation_id,
            timeout=min(remaining_deadline, remaining_signed_budget),
        )
        if not refreshed:
            return Result.failure(
                SafeErrorV1(
                    code="authentication_required",
                    message_key="credential_refresh_failed",
                    retryable=False,
                )
            )
        return Result.success(capability)


class UpdateRefreshReservationVerifierV1(Protocol):
    """Narrow durable authority lookup injected into the U1 refresh port."""

    def verify_update_refresh_reservation(
        self,
        reservation: UpdateCredentialRefreshReservationV1,
        *,
        user_id: int,
        session_id: str,
        conversation_id: str,
        credential_generation: int,
    ) -> bool: ...


class UpdateCredentialRefreshPortV1:
    """U1 identity boundary for one durable, operation-separated U3 refresh."""

    def __init__(self, verifier: UpdateRefreshReservationVerifierV1) -> None:
        self._verifier = verifier

    async def refresh_update_capability_once(
        self,
        capability: TicketRequestCapability,
        reservation: UpdateCredentialRefreshReservationV1,
        remaining_deadline: float,
    ) -> Result[TicketRequestCapability, SafeErrorV1]:
        if (
            remaining_deadline <= 0
            or reservation.refresh_state != "reserved"
            or not reservation.mutation_id
            or not reservation.refresh_reservation_id.startswith("update.")
            or reservation.base_credential_generation != capability.credential_generation
            or not self._verifier.verify_update_refresh_reservation(
                reservation,
                user_id=capability.user_id,
                session_id=capability.session_id,
                conversation_id=capability.conversation_id,
                credential_generation=capability.credential_generation,
            )
        ):
            return Result.failure(
                SafeErrorV1(
                    code="authentication_required",
                    message_key="credential_refresh_reservation_invalid",
                    retryable=False,
                )
            )
        refreshed = await capability._refresh_verified_once(
            reservation.refresh_reservation_id,
            timeout=remaining_deadline,
        )
        if not refreshed:
            return Result.failure(
                SafeErrorV1(
                    code="authentication_required",
                    message_key="credential_refresh_failed",
                    retryable=False,
                )
            )
        return Result.success(capability)
