"""Layered dependency-injection containers for the HTTP server stack.

Each layer is its own ``DeclarativeContainer`` and only sees the layer below
it, declared as a dependency rather than imported:

- :class:`CoreContainer`: process-wide inputs (the loaded config).
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
from voicegateway.repository.api_key_repository import ApiKeyRepository
from voicegateway.services.api_key_service import ApiKeyService

logger = logging.getLogger(__name__)


def _load_gateway_config() -> GatewayConfig:
    """Adapter so ``providers.Singleton`` can construct the config eagerly."""
    return GatewayConfig.load()


class CoreContainer(containers.DeclarativeContainer):
    """Process-wide inputs every other layer reads."""

    config = providers.Singleton(_load_gateway_config)


class InfraContainer(containers.DeclarativeContainer):
    """The database engine, one per process."""

    config = providers.Dependency(instance_of=GatewayConfig)

    database = providers.Singleton(Database, config=config)


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
    infra = providers.Container(InfraContainer, config=core.config)
    services = providers.Container(ServicesContainer, infra=infra)
