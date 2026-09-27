"""The DI containers are layered, and a missing layer input fails at startup."""

from __future__ import annotations

import pytest
from dependency_injector import containers, errors, providers

from voicegateway.core.config import GatewayConfig
from voicegateway.core.container import (
    Container,
    InfraContainer,
    ServicesContainer,
)
from voicegateway.core.database import Database


def test_config_override_reaches_the_infra_layer(tmp_path) -> None:
    cfg = GatewayConfig(cost_tracking={"db_path": str(tmp_path / "layer.db")})
    container = Container()
    container.core.config.override(providers.Object(cfg))
    container.check_dependencies()

    assert container.infra.database().config is cfg


def test_services_resolve_through_the_infra_layer(tmp_path) -> None:
    container = Container()
    container.infra.database.override(
        providers.Object(
            Database(GatewayConfig(cost_tracking={"db_path": str(tmp_path / "s.db")}))
        )
    )
    service = container.services.api_key_service()

    assert service is not container.services.api_key_service()  # Factory


def test_a_layer_without_its_input_fails_the_startup_check() -> None:
    class Broken(containers.DeclarativeContainer):
        infra = providers.Container(InfraContainer)  # no config passed
        services = providers.Container(ServicesContainer, infra=infra)

    with pytest.raises(errors.Error, match="config"):
        Broken().check_dependencies()
