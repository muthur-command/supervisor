"""Tests for MCOS dual-frontend bootstrap routing (landingpage ↔ mc_fd)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from awesomeversion import AwesomeVersion
import pytest

from supervisor.coresys import CoreSys
from supervisor.docker.mc_frontend import DockerMcFrontend
from supervisor.misc.mc_frontend_switch import FrontendRoute

from tests.docker.test_mc_stack import _capture_run_kwargs, _last_kwargs


@pytest.fixture
def stack_versions(coresys: CoreSys) -> None:
    """Populate updater state so MC stack flips into ``enabled``."""
    updater = coresys.updater
    updater._data["mc_bd"] = AwesomeVersion("0.1.0")  # noqa: SLF001
    updater._data["mc_fd"] = AwesomeVersion("0.1.0")  # noqa: SLF001
    updater._data["postgresql"] = AwesomeVersion("16.3")  # noqa: SLF001
    updater._data["redis"] = AwesomeVersion("7.2.4")  # noqa: SLF001
    updater._data["image"]["mc_bd"] = "ghcr.io/muthur-command/{arch}-mc-bd"  # noqa: SLF001
    updater._data["image"]["mc_fd"] = "ghcr.io/muthur-command/mc-fd"  # noqa: SLF001
    updater._data["image"]["postgresql"] = "docker.io/library/postgres"  # noqa: SLF001
    updater._data["image"]["redis"] = "docker.io/library/redis"  # noqa: SLF001


@pytest.fixture
def landingpage_versions(coresys: CoreSys, stack_versions: None) -> None:
    """Enable dual-frontend by providing a landingpage image template."""
    updater = coresys.updater
    updater._data["landingpage"] = AwesomeVersion("landingpage")  # noqa: SLF001
    updater._data["image"]["landingpage"] = (  # noqa: SLF001
        "ghcr.io/muthur-command/{machine}-landingpage"
    )


def test_dual_frontend_disabled_without_landingpage_image(
    coresys: CoreSys, stack_versions: None
) -> None:
    """Without a landingpage image template mc_fd keeps :8123 directly."""
    assert coresys.muthurcommand.unused is True
    assert coresys.mc_stack.dual_frontend is False
    assert coresys.mc_stack.frontend_switch.publish_mc_fd_host_port is True


def test_dual_frontend_disabled_without_landingpage_version(
    coresys: CoreSys, stack_versions: None
) -> None:
    """Image template alone is not enough; the version tag must be configured."""
    coresys.updater._data["image"]["landingpage"] = (  # noqa: SLF001
        "ghcr.io/muthur-command/{machine}-landingpage"
    )
    assert coresys.mc_stack.dual_frontend is False
    assert coresys.mc_stack.frontend_switch.publish_mc_fd_host_port is True


def test_dual_frontend_enabled_with_landingpage_image(
    coresys: CoreSys, landingpage_versions: None
) -> None:
    """Landingpage image + unused Core enables bootstrap routing."""
    switch = coresys.mc_stack.frontend_switch
    assert coresys.mc_stack.dual_frontend is True
    assert switch.route == FrontendRoute.LANDINGPAGE
    assert switch.publish_mc_fd_host_port is False


@pytest.mark.usefixtures("landingpage_versions", "tmp_supervisor_data", "path_extern")
async def test_mc_fd_skips_host_port_during_bootstrap(coresys: CoreSys) -> None:
    """mc_fd must not bind :8123 while landingpage owns the host port."""
    instance, run = _capture_run_kwargs(DockerMcFrontend, coresys)

    with (
        patch.object(DockerMcFrontend, "is_running", new=AsyncMock(return_value=False)),
        patch.object(DockerMcFrontend, "stop", new=AsyncMock()),
        patch.object(
            coresys.mc_stack,
            "dependency_extra_hosts",
            new=AsyncMock(return_value={}),
        ),
    ):
        await instance.run(publish_host_port=False)

    assert "ports" not in _last_kwargs(run)


@pytest.mark.usefixtures("landingpage_versions")
async def test_promote_mc_fd_switches_route(coresys: CoreSys) -> None:
    """Promotion stops landingpage, re-runs mc_fd with host port, persists route."""
    stack = coresys.mc_stack
    stack.frontend_switch.route = FrontendRoute.LANDINGPAGE

    with (
        patch.object(stack, "_stop_landingpage", new=AsyncMock()) as stop_lp,
        patch.object(stack.frontend, "stop", new=AsyncMock()),
        patch.object(stack.frontend, "run", new=AsyncMock()) as run_fd,
        patch.object(stack, "_check_frontend_ready", new=AsyncMock(return_value=True)),
        patch.object(stack.config, "save_data", new=AsyncMock()) as save,
    ):
        await stack._promote_mc_fd()  # noqa: SLF001

    stop_lp.assert_awaited_once()
    run_fd.assert_awaited_once_with(publish_host_port=True)
    assert stack.frontend_switch.route == FrontendRoute.MC_FD
    save.assert_awaited()


@pytest.mark.usefixtures("landingpage_versions")
async def test_fallback_to_landingpage(coresys: CoreSys) -> None:
    """Degraded mc_fd route falls back to landingpage on :8123."""
    stack = coresys.mc_stack
    stack.frontend_switch.route = FrontendRoute.MC_FD

    with (
        patch.object(stack.frontend, "stop", new=AsyncMock()),
        patch.object(stack, "_ensure_landingpage_running", new=AsyncMock()) as start_lp,
        patch.object(stack.config, "save_data", new=AsyncMock()),
    ):
        await stack.fallback_to_landingpage()

    assert stack.frontend_switch.route == FrontendRoute.LANDINGPAGE
    start_lp.assert_awaited_once()
