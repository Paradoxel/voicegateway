"""Layered dependency-injection containers for the HTTP server stack.

Each layer is its own ``DeclarativeContainer`` and only sees the layer below
it, declared as a dependency rather than imported:

- :class:`CoreContainer`: the process's ``Gateway`` and what it loaded.
- :class:`InfraContainer`: connections to the outside world (the database).
- :class:`ServicesContainer`: repositories and the services built on them.

:class:`Container` composes them. It is the only class callers touch, and
:meth:`Container.check_dependencies` fails fast at startup when a layer is
missing an input instead of on the first request that needs it.
"""

from __future__ import annotations

import logging

from dependency_injector import containers, providers

from voicegateway.core.config import GatewayConfig
from voicegateway.core.database import Database
from voicegateway.core.gateway import Gateway
from voicegateway.repository.api_key_repository import ApiKeyRepository
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


class ServicesContainer(containers.DeclarativeContainer):
    """Repositories and the services that own their business rules."""

    infra = providers.DependenciesContainer()

    api_key_repository = providers.Factory(
        ApiKeyRepository,
        session_factory=infra.database.provided.session,
    )
    api_key_service = providers.Factory(
        ApiKeyService,
        repository=api_key_repository,
    )


class Container(containers.DeclarativeContainer):
    """Application root: composes the layers and owns the wiring list."""

    wiring_config = containers.WiringConfiguration(
        modules=[
            "voicegateway.server.api.api_keys",
        ],
    )

    core = providers.Container(CoreContainer)
    infra = providers.Container(
        InfraContainer, config=core.config, storage=core.storage
    )
    services = providers.Container(ServicesContainer, infra=infra)
