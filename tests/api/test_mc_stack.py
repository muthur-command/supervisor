"""Tests for the MC stack REST API endpoints."""

# pylint: disable=protected-access

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient
from awesomeversion import AwesomeVersion
import pytest

from supervisor.coresys import CoreSys


@pytest.fixture
def stack_versions(coresys: CoreSys) -> None:
    """Populate updater state so all four MC stack components are configured."""
    updater = coresys.updater
    updater._data["mc_bd"] = AwesomeVersion("0.1.0")
    updater._data["mc_fd"] = AwesomeVersion("0.1.0")
    updater._data["postgresql"] = AwesomeVersion("16.3")
    updater._data["redis"] = AwesomeVersion("7.2.4")
    updater._data["image"]["mc_bd"] = "ghcr.io/muthur-command/{arch}-mc-bd"
    updater._data["image"]["mc_fd"] = "ghcr.io/muthur-command/mc-fd"
    updater._data["image"]["postgresql"] = "docker.io/library/postgres"
    updater._data["image"]["redis"] = "docker.io/library/redis"


@pytest.mark.usefixtures("stack_versions")
async def test_info_endpoint_lists_all_components(
    api_client: TestClient, coresys: CoreSys
) -> None:
    """``GET /mc_stack/info`` returns one entry per stack component."""
    resp = await api_client.get("/mc_stack/info")
    assert resp.status == 200
    body = await resp.json()
    data = body["data"]
    assert data["enabled"] is True
    for key in ("postgresql", "redis", "mc_bd", "mc_fd"):
        assert "name" in data[key]
        assert "image" in data[key]
        assert "state" in data[key]


async def test_restart_endpoint_dispatches_to_stack(
    api_client: TestClient, coresys: CoreSys
) -> None:
    """``POST /mc_stack/restart`` calls into ``MCStack.restart``."""
    with patch.object(coresys.mc_stack, "restart", new=AsyncMock()) as mock_restart:
        resp = await api_client.post("/mc_stack/restart")
    assert resp.status == 200
    mock_restart.assert_awaited_once()


async def test_update_endpoint_dispatches_to_stack(
    api_client: TestClient, coresys: CoreSys
) -> None:
    """``POST /mc_stack/update`` calls into ``MCStack.update``."""
    with patch.object(coresys.mc_stack, "update", new=AsyncMock()) as mock_update:
        resp = await api_client.post("/mc_stack/update")
    assert resp.status == 200
    mock_update.assert_awaited_once()
