"""One SQLAlchemy engine per server process.

``Container.database`` used to build its own ``Database`` from the config while
``StorageService`` held another on the same file, so the API-key routes and
everything else ran on two connection pools against one SQLite database.
"""

from __future__ import annotations

from voicegateway.core.gateway import Gateway
from voicegateway.server import build_app


def test_container_reuses_the_storage_engine(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("VOICEGW_DB_PATH", str(tmp_path / "one.db"))
    gw = Gateway(require_config=False)
    app = build_app(gw, enable_mcp_sse=False, enable_dashboard=False)

    assert app.state.container.infra.database() is gw.storage.database


def test_container_builds_its_own_engine_without_storage(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VOICEGW_DB_PATH", raising=False)
    monkeypatch.delenv("VOICEGW_DB_URL", raising=False)
    monkeypatch.setattr("voicegateway.core.config._CONFIG_PATHS", [tmp_path / "none.yaml"])
    gw = Gateway(require_config=False)
    assert gw.storage is None
    app = build_app(gw, enable_mcp_sse=False, enable_dashboard=False)

    assert app.state.container.infra.database() is not None
