"""Tests for A1 plan stages 4 / 5 / 6.

Stage 4: ``MCStack`` orchestration polish — differentiated update strategies,
``healthcheck()`` consumed by Resolution / API.

Stage 5: Muthur Command Core watchdog short-circuits when the MCOS image marks Home
Assistant Core as ``unused``; MC stack watchdog policy escalates
``mc_bd → stack`` and never touches data volumes.

Stage 6: Resolution evaluates MC stack staleness; ``MuthurCommandCore``
version evaluator no-ops when Core is unused; ``/info`` and
``/available_updates`` surface MC stack data; sentry diagnostic context
includes MC stack versions.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from awesomeversion import AwesomeVersion
import pytest

from supervisor.const import CoreState
from supervisor.coresys import CoreSys
from supervisor.docker.const import ContainerState
from supervisor.docker.mc_backend import DockerMcBackend
from supervisor.docker.muthurcommand import DockerMuthurCommand
from supervisor.exceptions import DockerError, MCStackUpdateError
from supervisor.misc.filter import filter_data
from supervisor.misc.mc_stack import MCStackComponentHealth, MCStackUpdateStrategy
from supervisor.misc.tasks import MC_STACK_WATCHDOG_API_FAILURES, Tasks
from supervisor.resolution.const import UnsupportedReason
from supervisor.resolution.evaluations.mc_stack_version import EvaluateMCStackVersion
from supervisor.resolution.evaluations.muthurcommand_core_version import (
    EvaluateMuthurCommandCoreVersion,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Stage 4: differentiated update strategies + healthcheck()
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("stack_versions")
async def test_update_skips_components_with_matching_versions(coresys: CoreSys) -> None:
    """No upgrade is attempted for components already at the desired version."""
    stack = coresys.mc_stack
    for inst, version in (
        (stack.postgres, AwesomeVersion("16.3")),
        (stack.redis, AwesomeVersion("7.2.4")),
        (stack.backend, AwesomeVersion("0.1.0")),
        (stack.frontend, AwesomeVersion("0.1.0")),
    ):
        with patch.object(
            type(inst), "version", new=property(lambda self, v=version: v)
        ):
            pass  # we patch through individual asserts below

    update_calls = [AsyncMock() for _ in stack.components]
    restart = AsyncMock()
    with (
        patch.object(stack.postgres, "update", update_calls[0]),
        patch.object(stack.redis, "update", update_calls[1]),
        patch.object(stack.backend, "update", update_calls[2]),
        patch.object(stack.frontend, "update", update_calls[3]),
        patch.object(stack, "restart", restart),
        patch.object(
            type(stack.postgres),
            "version",
            new=property(lambda self: AwesomeVersion("16.3")),
        ),
        patch.object(
            type(stack.redis),
            "version",
            new=property(lambda self: AwesomeVersion("7.2.4")),
        ),
        patch.object(
            type(stack.backend),
            "version",
            new=property(lambda self: AwesomeVersion("0.1.0")),
        ),
        patch.object(
            type(stack.frontend),
            "version",
            new=property(lambda self: AwesomeVersion("0.1.0")),
        ),
    ):
        await stack.update()

    for call in update_calls:
        call.assert_not_awaited()
    restart.assert_not_awaited()


@pytest.mark.usefixtures("stack_versions")
async def test_update_invokes_components_when_versions_diverge(
    coresys: CoreSys,
) -> None:
    """``update()`` only touches components whose desired version changed."""
    stack = coresys.mc_stack
    # Force "current" versions to diverge from the updater's "latest".
    with (
        patch.object(
            type(stack.postgres),
            "version",
            new=property(lambda self: AwesomeVersion("16.0")),
        ),
        patch.object(
            type(stack.redis),
            "version",
            new=property(lambda self: AwesomeVersion("7.2.4")),
        ),
        patch.object(
            type(stack.backend),
            "version",
            new=property(lambda self: AwesomeVersion("0.0.9")),
        ),
        patch.object(
            type(stack.frontend),
            "version",
            new=property(lambda self: AwesomeVersion("0.1.0")),
        ),
        patch.object(stack.postgres, "update", new=AsyncMock()) as pg_update,
        patch.object(stack.redis, "update", new=AsyncMock()) as redis_update,
        patch.object(stack.backend, "update", new=AsyncMock()) as bd_update,
        patch.object(stack.frontend, "update", new=AsyncMock()) as fd_update,
        patch.object(stack, "restart", new=AsyncMock()) as restart,
    ):
        await stack.update()

    # Stale postgres + mc_bd → updated; redis + mc_fd → skipped.
    pg_update.assert_awaited_once()
    bd_update.assert_awaited_once()
    redis_update.assert_not_awaited()
    fd_update.assert_not_awaited()
    restart.assert_awaited_once()


@pytest.mark.usefixtures("stack_versions")
async def test_update_failure_wraps_as_mc_stack_update_error(coresys: CoreSys) -> None:
    """A DockerError during update surfaces as ``MCStackUpdateError``."""
    stack = coresys.mc_stack
    with (
        patch.object(
            type(stack.postgres),
            "version",
            new=property(lambda self: AwesomeVersion("16.0")),
        ),
        patch.object(
            stack.postgres,
            "update",
            new=AsyncMock(side_effect=DockerError("pull blew up")),
        ),
        pytest.raises(MCStackUpdateError),
    ):
        await stack.update()


def test_update_strategy_enum_values() -> None:
    """Strategies are stable string values for diagnostic / logging use."""
    assert MCStackUpdateStrategy.ROLLING_RECREATE.value == "rolling_recreate"
    assert MCStackUpdateStrategy.TAG_SWAP_RESTART.value == "tag_swap_restart"


@pytest.mark.usefixtures("stack_versions")
async def test_healthcheck_returns_per_component_snapshot(coresys: CoreSys) -> None:
    """``healthcheck()`` mixes container state + readiness probe per component."""
    stack = coresys.mc_stack
    with (
        patch.object(
            type(stack.postgres),
            "current_state",
            new=AsyncMock(return_value=ContainerState.RUNNING),
        ),
        patch.object(
            type(stack.redis),
            "current_state",
            new=AsyncMock(return_value=ContainerState.RUNNING),
        ),
        patch.object(
            type(stack.backend),
            "current_state",
            new=AsyncMock(return_value=ContainerState.FAILED),
        ),
        patch.object(
            type(stack.frontend),
            "current_state",
            new=AsyncMock(return_value=ContainerState.RUNNING),
        ),
        patch.object(stack, "_check_postgres_ready", new=AsyncMock(return_value=True)),
        patch.object(stack, "_check_redis_ready", new=AsyncMock(return_value=True)),
        patch.object(stack, "_check_backend_ready", new=AsyncMock(return_value=False)),
        patch.object(stack, "_check_frontend_ready", new=AsyncMock(return_value=True)),
    ):
        snapshot = await stack.healthcheck()

    assert {h.name for h in snapshot.values()} == {
        stack.postgres.name,
        stack.redis.name,
        stack.backend.name,
        stack.frontend.name,
    }
    assert all(isinstance(v, MCStackComponentHealth) for v in snapshot.values())
    pg = snapshot[stack.postgres.name]
    assert pg.healthy is True and pg.degraded is False
    bd = snapshot[stack.backend.name]
    # backend FAILED → never asks the readiness probe, marked degraded
    assert bd.healthy is False and bd.degraded is True


async def test_healthcheck_returns_empty_when_disabled(coresys: CoreSys) -> None:
    """No version data → no healthcheck output."""
    assert coresys.mc_stack.enabled is False
    assert await coresys.mc_stack.healthcheck() == {}


# ---------------------------------------------------------------------------
# Stage 5: Muthur Command Core watchdog short-circuits + MC stack policy
# ---------------------------------------------------------------------------


def test_muthurcommand_unused_signal(coresys: CoreSys) -> None:
    """``MuthurCommand.unused`` is True only with neither current nor latest version."""
    coresys.muthurcommand.version = None
    coresys.updater._data["muthurcommand"] = None  # noqa: SLF001
    assert coresys.muthurcommand.unused is True

    coresys.muthurcommand.version = AwesomeVersion("2024.1.0")
    assert coresys.muthurcommand.unused is False

    coresys.muthurcommand.version = None
    coresys.updater._data["muthurcommand"] = AwesomeVersion("2024.1.0")  # noqa: SLF001
    assert coresys.muthurcommand.unused is False


async def test_ha_watchdog_skipped_when_unused(coresys: CoreSys) -> None:
    """``_watchdog_muthurcommand_api`` returns immediately on MCOS-only images."""
    tasks = Tasks(coresys)
    # `unused` is true by default in the conftest setup.
    assert coresys.muthurcommand.unused is True
    with patch.object(
        coresys.muthurcommand.api, "check_api_state", new=AsyncMock()
    ) as probe:
        await tasks._watchdog_muthurcommand_api()  # noqa: SLF001
    probe.assert_not_called()


async def test_ha_core_start_skipped_when_unused(coresys: CoreSys) -> None:
    """``MuthurCommandCore.start`` no-ops when Core is unused on MCOS-only images."""
    assert coresys.muthurcommand.unused is True
    with (
        patch.object(DockerMuthurCommand, "run", new=AsyncMock()) as run,
        patch.object(
            DockerMuthurCommand, "is_running", new=AsyncMock(return_value=False)
        ),
    ):
        await coresys.muthurcommand.core.start()
    run.assert_not_called()


@pytest.mark.usefixtures("stack_versions")
async def test_mc_stack_watchdog_resets_failure_counter_on_recovery(
    coresys: CoreSys,
) -> None:
    """The MC-stack watchdog forgets prior misses once mc_bd answers again."""
    tasks = Tasks(coresys)
    tasks._cache[MC_STACK_WATCHDOG_API_FAILURES] = 1  # noqa: SLF001

    with (
        patch.object(DockerMcBackend, "is_running", new=AsyncMock(return_value=True)),
        patch.object(
            coresys.mc_stack,
            "_check_backend_ready",
            new=AsyncMock(return_value=True),
        ),
    ):
        await tasks._watchdog_mc_stack()  # noqa: SLF001

    assert tasks._cache[MC_STACK_WATCHDOG_API_FAILURES] == 0  # noqa: SLF001


@pytest.mark.usefixtures("stack_versions")
async def test_mc_stack_watchdog_first_tier_restart_only(coresys: CoreSys) -> None:
    """Two consecutive misses trigger a container restart, not a full stack reboot."""
    tasks = Tasks(coresys)
    tasks._cache[MC_STACK_WATCHDOG_API_FAILURES] = 1  # noqa: SLF001  # one miss already

    with (
        patch.object(DockerMcBackend, "is_running", new=AsyncMock(return_value=True)),
        patch.object(
            coresys.mc_stack,
            "_check_backend_ready",
            new=AsyncMock(return_value=False),
        ),
        patch.object(DockerMcBackend, "restart", new=AsyncMock()) as backend_restart,
        patch.object(coresys.mc_stack, "restart", new=AsyncMock()) as stack_restart,
    ):
        await tasks._watchdog_mc_stack()  # noqa: SLF001

    backend_restart.assert_awaited_once()
    stack_restart.assert_not_awaited()


@pytest.mark.usefixtures("stack_versions")
async def test_mc_stack_watchdog_escalates_to_stack_when_backend_dead(
    coresys: CoreSys,
) -> None:
    """When mc_bd container is dead, watchdog falls back to a full stack restart."""
    tasks = Tasks(coresys)
    tasks._cache[MC_STACK_WATCHDOG_API_FAILURES] = 1  # noqa: SLF001

    with (
        patch.object(DockerMcBackend, "is_running", new=AsyncMock(return_value=True)),
        patch.object(
            coresys.mc_stack,
            "_check_backend_ready",
            new=AsyncMock(return_value=False),
        ),
        patch.object(
            DockerMcBackend,
            "restart",
            new=AsyncMock(side_effect=DockerError("boom")),
        ) as backend_restart,
        patch.object(
            DockerMcBackend,
            "current_state",
            new=AsyncMock(return_value=ContainerState.FAILED),
        ),
        patch.object(coresys.mc_stack, "restart", new=AsyncMock()) as stack_restart,
    ):
        await tasks._watchdog_mc_stack()  # noqa: SLF001

    backend_restart.assert_awaited_once()
    stack_restart.assert_awaited_once()


# ---------------------------------------------------------------------------
# Stage 6: Resolution / API / Filter
# ---------------------------------------------------------------------------


async def test_evaluate_ha_core_version_skips_when_unused(coresys: CoreSys) -> None:
    """The Muthur Command Core staleness eval no-ops on MCOS images that omit Muthur Command."""
    eval_obj = EvaluateMuthurCommandCoreVersion(coresys)
    assert coresys.muthurcommand.unused is True
    assert await eval_obj.evaluate() is False


@pytest.mark.usefixtures("stack_versions")
async def test_evaluate_mc_stack_version_does_not_flag_recent_version(
    coresys: CoreSys,
) -> None:
    """A fresh-versions stack is not flagged as unsupported."""
    stack = coresys.mc_stack
    with (
        patch.object(
            type(stack.backend),
            "version",
            new=property(lambda self: AwesomeVersion("0.1.0")),
        ),
        patch.object(
            type(stack.frontend),
            "version",
            new=property(lambda self: AwesomeVersion("0.1.0")),
        ),
    ):
        assert await EvaluateMCStackVersion(coresys).evaluate() is False


@pytest.mark.usefixtures("stack_versions")
async def test_evaluate_mc_stack_version_flags_stale_calver(coresys: CoreSys) -> None:
    """A 2-year-old CalVer mc_bd version is flagged as unsupported."""
    coresys.updater._data["mc_bd"] = AwesomeVersion("2026.4.0")  # noqa: SLF001
    stack = coresys.mc_stack
    with (
        patch.object(
            type(stack.backend),
            "version",
            new=property(lambda self: AwesomeVersion("2024.1.0")),
        ),
        patch.object(
            type(stack.frontend),
            "version",
            new=property(lambda self: AwesomeVersion("0.1.0")),
        ),
    ):
        assert await EvaluateMCStackVersion(coresys).evaluate() is True


def test_unsupported_reason_includes_mc_stack(coresys: CoreSys) -> None:
    """``UnsupportedReason`` exposes the new ``mc_stack_version`` enum value."""
    assert UnsupportedReason.MC_STACK_VERSION.value == "mc_stack_version"


@pytest.mark.usefixtures("stack_versions")
async def test_root_info_includes_mc_stack_versions(api_client, coresys) -> None:
    """``GET /info`` surfaces MC stack versions for clients."""
    resp = await api_client.get("/info")
    assert resp.status == 200
    data = (await resp.json())["data"]

    assert "mc_stack" in data
    stack = data["mc_stack"]
    assert stack["enabled"] is True
    for key in ("mc_bd", "mc_fd", "postgresql", "redis"):
        assert key in stack
        assert "version_latest" in stack[key]


@pytest.mark.usefixtures("stack_versions")
async def test_root_available_updates_lists_mc_stack(
    api_client, coresys: CoreSys
) -> None:
    """``GET /available_updates`` lists each MC component that lags behind."""
    coresys.updater._data["mc_bd"] = AwesomeVersion("2026.4.0")  # noqa: SLF001
    with patch.object(
        type(coresys.mc_stack.backend),
        "version",
        new=property(lambda self: AwesomeVersion("2024.1.0")),
    ):
        resp = await api_client.get("/available_updates")
        data = (await resp.json())["data"]["available_updates"]

    update_types = [item["update_type"] for item in data]
    assert "mc_bd" in update_types


@pytest.mark.usefixtures("stack_versions")
async def test_filter_data_includes_mc_stack_versions(coresys: CoreSys) -> None:
    """Sentry diagnostic context carries the MC stack versions."""
    coresys.config.diagnostics = True
    await coresys.core.set_state(CoreState.RUNNING)

    event = {"contexts": {}, "tags": {}}
    with patch.object(coresys.hardware.disk, "get_disk_free_space", return_value=12345):
        out = filter_data(coresys, event, {})
    assert out is not None
    versions = out["contexts"]["versions"]
    assert "mc_bd" in versions
    assert "mc_fd" in versions
    assert "postgresql" in versions
    assert "redis" in versions
    assert out["contexts"]["mc_stack"]["enabled"] is True


@pytest.mark.usefixtures("stack_versions")
async def test_mc_stack_health_endpoint(api_client, coresys: CoreSys) -> None:
    """``GET /mc_stack/health`` returns the per-component health snapshot."""
    with patch.object(
        coresys.mc_stack,
        "healthcheck",
        new=AsyncMock(
            return_value={
                "mcos_mc_postgres": MCStackComponentHealth(
                    "mcos_mc_postgres", ContainerState.RUNNING, True
                ),
                "mcos_mc_redis": MCStackComponentHealth(
                    "mcos_mc_redis", ContainerState.RUNNING, True
                ),
                "mcos_mc_bd": MCStackComponentHealth(
                    "mcos_mc_bd", ContainerState.FAILED, False
                ),
                "mcos_mc_fd": MCStackComponentHealth(
                    "mcos_mc_fd", ContainerState.RUNNING, True
                ),
            }
        ),
    ):
        resp = await api_client.get("/mc_stack/health")

    assert resp.status == 200
    data = (await resp.json())["data"]
    assert data["mcos_mc_bd"]["healthy"] is False
    assert data["mcos_mc_postgres"]["healthy"] is True


async def test_resolution_auto_loads_mc_stack_version_eval(coresys: CoreSys) -> None:
    """The evaluator-loader autodiscovers the new MC stack version eval."""
    await coresys.resolution.load_modules()
    slugs = {ev.slug for ev in coresys.resolution.evaluate.all_evaluations}
    assert "mc_stack_version" in slugs


@pytest.mark.usefixtures("stack_versions")
async def test_mc_fd_web_proxy_returns_503_when_disabled(
    api_client, coresys: CoreSys
) -> None:
    """``/mc_fd/web/...`` returns 503 if the stack is administratively off."""
    # Strip versions so ``MCStack.enabled`` flips back to False.
    coresys.updater._data["mc_bd"] = None  # noqa: SLF001
    resp = await api_client.get("/mc_fd/web/login")
    assert resp.status == 503


async def test_ingress_update_core_panel_skipped_when_unused(coresys: CoreSys) -> None:
    """``Ingress.update_core_panel`` short-circuits when Muthur Command Core is unused.

    The MCOS-only image does not expose HA's ``mcos_push/panel`` API,
    so the ingress manager must never attempt the call.
    """
    assert coresys.muthurcommand.unused is True

    addon = AsyncMock()
    addon.slug = "demo"
    addon.ingress_panel = True

    with patch.object(
        coresys.muthurcommand.api,
        "make_request",
        new=AsyncMock(),
    ) as make_request:
        await coresys.ingress.update_core_panel(addon)

    make_request.assert_not_called()
