"""Tests for the MCStack orchestrator."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from awesomeversion import AwesomeVersion
import pytest

from supervisor.coresys import CoreSys
from supervisor.docker.const import ContainerState
from supervisor.docker.mc_backend import DockerMcBackend
from supervisor.docker.mc_frontend import DockerMcFrontend
from supervisor.docker.mc_postgres import DockerMcPostgres
from supervisor.docker.mc_redis import DockerMcRedis
from supervisor.exceptions import MCStackStartupError


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


def test_enabled_requires_all_versions(coresys: CoreSys) -> None:
    """The stack only activates when all four images and versions are set."""
    assert coresys.mc_stack.enabled is False


@pytest.mark.usefixtures("stack_versions")
def test_enabled_when_all_versions_present(coresys: CoreSys) -> None:
    """All four versions configured flips the stack into ``enabled``."""
    assert coresys.mc_stack.enabled is True


async def test_start_skips_when_disabled(coresys: CoreSys) -> None:
    """``start`` is a no-op when the stack isn't fully configured."""
    with patch.object(
        coresys.mc_stack, "_start_component", new=AsyncMock()
    ) as mock_start:
        await coresys.mc_stack.start()
    mock_start.assert_not_called()


@pytest.mark.usefixtures("stack_versions")
async def test_start_runs_each_component_in_order(coresys: CoreSys) -> None:
    """Components must be started in: postgres → redis → mc_bd → mc_fd order."""
    order: list[str] = []

    async def record(inst, *, timeout, health):
        order.append(inst.name)

    with patch.object(coresys.mc_stack, "_start_component", side_effect=record):
        await coresys.mc_stack.start()

    assert order == [
        coresys.mc_stack.postgres.name,
        coresys.mc_stack.redis.name,
        coresys.mc_stack.backend.name,
        coresys.mc_stack.frontend.name,
    ]


@pytest.mark.usefixtures("stack_versions")
async def test_stop_calls_components_in_reverse(coresys: CoreSys) -> None:
    """``stop`` tears down components in reverse dependency order."""
    order: list[str] = []

    async def fake_stop(self_inst, *, remove_container=False):  # noqa: ARG001
        order.append(self_inst.name)

    with (
        patch.object(DockerMcPostgres, "stop", autospec=True, side_effect=fake_stop),
        patch.object(DockerMcRedis, "stop", autospec=True, side_effect=fake_stop),
        patch.object(DockerMcBackend, "stop", autospec=True, side_effect=fake_stop),
        patch.object(DockerMcFrontend, "stop", autospec=True, side_effect=fake_stop),
    ):
        await coresys.mc_stack.stop()

    assert order == [
        coresys.mc_stack.frontend.name,
        coresys.mc_stack.backend.name,
        coresys.mc_stack.redis.name,
        coresys.mc_stack.postgres.name,
    ]


@pytest.mark.usefixtures("stack_versions")
async def test_start_component_propagates_failed_container(coresys: CoreSys) -> None:
    """A container that dies before becoming healthy raises a startup error."""
    with (
        patch.object(DockerMcPostgres, "is_running", new=AsyncMock(return_value=True)),
        patch.object(
            DockerMcPostgres,
            "current_state",
            new=AsyncMock(return_value=ContainerState.FAILED),
        ),
        patch.object(
            coresys.mc_stack,
            "_check_postgres_ready",
            new=AsyncMock(return_value=False),
        ),
        pytest.raises(MCStackStartupError),
    ):
        await coresys.mc_stack._start_component(  # noqa: SLF001
            coresys.mc_stack.postgres,
            timeout=__import__("datetime").timedelta(seconds=1),
            health=coresys.mc_stack._check_postgres_ready,  # noqa: SLF001
        )
