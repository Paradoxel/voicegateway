"""Layered dependency-injection containers for the HTTP server stack.

Each layer is its own ``DeclarativeContainer`` and only sees the layer below
it, declared as a dependency rather than imported:

- :class:`CoreContainer`: the process's ``Gateway`` and what it loaded.
- :class:`InfraContainer`: connections to the outside world (the database).
- :class:`ServicesContainer`: services over the function-style repositories.

:class:`Container` composes them. It is the only class callers touch, and
:meth:`Container.check_dependencies` fails fast at startup when a layer is
missing an input instead of on the first request that needs it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from dependency_injector import containers, providers
from sqlalchemy.ext.asyncio import AsyncSession

from voicegateway.core.config import GatewayConfig
from voicegateway.core.database import Database
from voicegateway.core.gateway import Gateway
from voicegateway.services.api_key_service import ApiKeyService
from voicegateway.services.storage_service import StorageService

logger = logging.getLogger(__name__)


def _database(storage: StorageService | None, config: GatewayConfig) -> Database:
    """The storage facade's engine when storage is on, else a fresh one.

    Reusing the facade's engine is what keeps a server process on one
    connection pool; building a second ``Database`` on the same file is the
    bug this replaced.
    """
    return storage.database if storage is not None else Database(config)


def _session_factory(
    storage: StorageService | None, database: Database
) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    """``StorageService.session`` when storage is on, else the bare engine's.

    The storage facade runs migrations before its first session, so services
    never meet a fresh database without its tables. The bare engine is only
    reached when cost tracking is off and there is no facade.
    """
    return storage.session if storage is not None else database.session


class CoreContainer(containers.DeclarativeContainer):
    """The process's ``Gateway`` and what it loaded.

    The ``Gateway`` is an input, not something this container builds: every
    entry point (``voicegw serve``, the tests, the MCP transport) already holds
    one, and it must be the same instance the routes read from ``app.state``.
    ``config`` and ``storage`` resolve through it on every call, so a
    ``refresh_config`` after a managed-config write is visible here too.
    """

    gateway = providers.Dependency(instance_of=Gateway)

    config = gateway.provided.config
    storage = gateway.provided.storage


class InfraContainer(containers.DeclarativeContainer):
    """The database engine, one per process."""

    config = providers.Dependency(instance_of=GatewayConfig)
    # ``None`` when cost tracking is off, so no ``instance_of`` narrower than
    # object: the check would reject the legitimate "no storage" case.
    storage: providers.Dependency[StorageService | None] = providers.Dependency()

    database = providers.Singleton(_database, storage=storage, config=config)

    session_factory = providers.Callable(
        _session_factory, storage=storage, database=database
    )


class ServicesContainer(containers.DeclarativeContainer):
    """Services that own business rules over the repository modules."""

    infra = providers.DependenciesContainer()

    api_key_service = providers.Factory(
        ApiKeyService,
        session_factory=infra.session_factory,
    )


class Container(containers.DeclarativeContainer):
    """Application root: composes the layers and owns the wiring list."""

    wiring_config = containers.WiringConfiguration(
        modules=[
            "voicegateway.server.api.api_keys",
        ],
        # A string marker naming a provider that does not exist warns at wire
        # time instead of failing on the first request that resolves it.
        warn_unresolved=True,
    )

    core = providers.Container(CoreContainer)
    infra = providers.Container(
        InfraContainer, config=core.config, storage=core.storage
    )
    services = providers.Container(ServicesContainer, infra=infra)
