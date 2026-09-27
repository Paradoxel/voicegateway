"""Service for issuing, verifying and revoking virtual keys.

A thin session-owning facade over :mod:`voicegateway.repository.api_keys_repository`,
the single implementation of the ``api_keys`` table. Hashing, prefixes, scope
normalization and the wildcard refusal all live there, so the ``/v1/api-keys``
routes, the dashboard, the CLI and request authentication share one code path.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING

from voicegateway.core.exceptions import NotFoundError
from voicegateway.repository import api_keys_repository as repo
from voicegateway.repository.api_keys_repository import (
    ApiKeyRow,
    CreatedApiKey,
    VerifiedKey,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

SessionFactory = Callable[[], AbstractAsyncContextManager["AsyncSession"]]


class ApiKeyService:
    """Opens a session per call and delegates to the api-keys repository."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session = session_factory

    async def create_key(
        self,
        *,
        name: str,
        scopes: str,
        tenant_id: str | None = None,
        issued_by: str | None = None,
        project_ids: str | None = None,
    ) -> CreatedApiKey:
        """Mint a new virtual key. The plaintext is returned exactly once.

        Raises ``ValueError`` for an empty name or a refused scope list,
        including the wildcard (VG-SEC-006).
        """
        async with self._session() as s:
            return await repo.create_api_key(
                s,
                name=name,
                scopes=scopes,
                tenant_id=tenant_id,
                issued_by=issued_by,
                project_ids=project_ids,
            )

    async def get_by_id(self, key_id: int) -> ApiKeyRow:
        """Return one key. Raises :class:`NotFoundError` when missing."""
        async with self._session() as s:
            row = await repo.get_by_id(s, key_id)
        if row is None:
            raise NotFoundError(detail=f"ApiKey {key_id} not found")
        return row

    async def list_keys(self, *, include_revoked: bool = True) -> list[ApiKeyRow]:
        """Return all keys, newest first."""
        async with self._session() as s:
            return await repo.list_keys(s, include_revoked=include_revoked)

    async def verify(self, plaintext: str) -> VerifiedKey | None:
        """Validate a plaintext key against the stored hashes."""
        async with self._session() as s:
            return await repo.verify(s, plaintext)

    async def mark_used(self, key_id: int) -> None:
        """Bump last_used_at on the row. Idempotent."""
        async with self._session() as s:
            await repo.mark_used(s, key_id)

    async def revoke(self, key_id: int) -> bool:
        """Soft-revoke. 404 if the row is missing, else True on the transition."""
        await self.get_by_id(key_id)
        async with self._session() as s:
            return await repo.revoke(s, key_id)

    async def list_stale(self, *, stale_after_days: int) -> list[ApiKeyRow]:
        """Non-revoked keys past the staleness cutoff."""
        async with self._session() as s:
            return await repo.list_stale(s, stale_after_days=stale_after_days)
