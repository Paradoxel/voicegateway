"""Security contract vocabulary for the live API: enums and the route matrix.

This module is inert. It imports nothing from ``voicegateway.server`` or
``voicegateway.repository``, and the only thing production imports from it is
:class:`PrincipalKind`. The rest describes the routes that ship today:

- :class:`ScopeName`, mirrored by ``core.scopes`` (``tests/core/test_scopes.py``
  asserts the two agree),
- :class:`AuthorizationRule` and :class:`AuthorizationMatrix`, one row per live
  route, loaded from ``authorization_matrix.json`` and held to the real app by
  ``tests/server/test_telemetry_authorization_matrix.py``.
"""

from __future__ import annotations

import json
from enum import StrEnum
from importlib import resources
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Package that carries ``authorization_matrix.json``, resolved via
#: ``importlib.resources`` exactly like ``provider_baselines.json``.
_DATA_PACKAGE = "voicegateway.schemas.telemetry"

#: A label for a known open defect, e.g. ``VG-SEC-004``. Shared by the matrix
#: rows and the cross-tenant fixtures so both name the same defect the same way.
GAP_ID_PATTERN = r"^VG-SEC-\d{3}$"

GapId = Annotated[str, Field(pattern=GAP_ID_PATTERN)]


class ContractStatus(StrEnum):
    """How a matrix row relates to what production does today."""

    #: Production already meets the row. Probed by a conformance test.
    ENFORCED = "enforced"
    #: The route exists and production deviates from the contract.
    GAP = "gap"


class ScopeName(StrEnum):
    """Scope names, matching ``core.scopes.ALL`` one for one."""

    #: Config mutation: providers, models, projects, rate-card rules.
    WRITE = "write"
    #: Exists as ``READ_SCOPE`` but is consulted only on the static-key branch.
    READ = "read"
    #: Exists, and is checked two ways (``role`` column and ``scopes`` CSV).
    ADMIN = "admin"
    #: Matches every check. Refused at mint from 0.26.0.
    WILDCARD = "*"
    #: Telemetry ingest only; never config mutation.
    INGEST = "ingest"
    #: Mintable today, but no route requires it yet: MCP auth is one shared
    #: static token.
    MCP_READ = "mcp:read"


class PrincipalKind(StrEnum):
    """The kinds of caller the auth layer can resolve."""

    #: No credential, or a static config key. Resolves to a full admin.
    OPERATOR = "operator"
    #: A ``vk_`` key whose ``role`` is ``admin``. May span tenants.
    ADMIN_KEY = "admin_key"
    #: A ``vk_`` key whose ``role`` is ``tenant``. Bound to one tenant.
    TENANT_KEY = "tenant_key"
    #: A token listed in ``auth.api_keys`` in the YAML config.
    STATIC_KEY = "static_key"
    #: The shared ``VOICEGW_MCP_TOKEN``. Carries no per-caller identity.
    MCP_TOKEN = "mcp_token"


class RouteAuth(StrEnum):
    """How a route is gated, as recovered from the FastAPI dependency graph."""

    #: No auth dependency resolves on this route.
    OPEN = "open"
    #: Guarded by ``require_principal``.
    PRINCIPAL = "principal"
    #: Guarded by both ``require_principal`` and ``require_scope("admin")``.
    PRINCIPAL_SCOPE_ADMIN = "principal+scope:admin"
    #: Guarded by ``require_scope("write")``.
    SCOPE_WRITE = "scope:write"
    #: Guarded by ``require_scope("admin")``.
    SCOPE_ADMIN = "scope:admin"
    #: Guarded by ``require_ingest_principal``, which enforces the ingest
    #: scope and yields the Principal the handler writes rows under.
    SCOPE_INGEST = "scope:ingest"


class AuthorizationRule(BaseModel):
    """One ``(method, path)`` row of the authorization matrix.

    ``status`` answers one question: *does this route, as shipped, meet its
    contract?* A ``GAP`` row must name the known defect by ``gap_id``; an
    ``ENFORCED`` row carries none.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    method: Literal["GET", "POST", "PATCH", "PUT", "DELETE"]
    path: str
    auth: RouteAuth
    status: ContractStatus
    #: True when the row's contract requires the response be filtered to the
    #: caller's tenant. False for genuinely global routes such as ``/health``.
    tenant_scoped: bool
    gap_id: GapId | None = None
    note: str = ""

    @model_validator(mode="after")
    def _gap_rows_are_labelled(self) -> AuthorizationRule:
        """A gap row names its defect; an enforced row names none."""
        if self.status is ContractStatus.ENFORCED and self.gap_id is not None:
            raise ValueError(
                f"{self.method} {self.path}: an enforced row carries no gap_id"
            )
        if self.status is ContractStatus.GAP and self.gap_id is None:
            raise ValueError(f"{self.method} {self.path}: a gap row requires a gap_id")
        return self

    @property
    def key(self) -> tuple[str, str]:
        """The ``(method, path)`` identity used for the bijection test."""
        return (self.method, self.path)


class AuthorizationMatrix(BaseModel):
    """Exactly one row per live API route."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    routes: list[AuthorizationRule]

    def keys(self) -> set[tuple[str, str]]:
        """Return every ``(method, path)`` covered by a row."""
        return {rule.key for rule in self.routes}

    def by_key(self) -> dict[tuple[str, str], AuthorizationRule]:
        """Index the rows by ``(method, path)``."""
        return {rule.key: rule for rule in self.routes}

    @model_validator(mode="after")
    def _keys_are_unique(self) -> AuthorizationMatrix:
        """No duplicate rows."""
        live = [r.key for r in self.routes]
        if len(live) != len(set(live)):
            dupes = sorted({k for k in live if live.count(k) > 1})
            raise ValueError(f"duplicate matrix rows: {dupes}")
        return self


def load_authorization_matrix() -> AuthorizationMatrix:
    """Load and validate ``authorization_matrix.json``."""
    raw = (
        resources.files(_DATA_PACKAGE)
        .joinpath("authorization_matrix.json")
        .read_text(encoding="utf-8")
    )
    return AuthorizationMatrix.model_validate(json.loads(raw))


__all__ = [
    "AuthorizationMatrix",
    "AuthorizationRule",
    "ContractStatus",
    "GAP_ID_PATTERN",
    "GapId",
    "PrincipalKind",
    "RouteAuth",
    "ScopeName",
    "load_authorization_matrix",
]
