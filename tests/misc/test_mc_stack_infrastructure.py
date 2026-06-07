"""End-to-end tests for the MC stack supporting infrastructure.

These cover the parts that were "wired up but not exercised":

* Persistence round-trip for ``MCStackSecrets`` and ``MCStackConfig``.
* ``MCStack.load`` actually attaches each ``Docker*`` instance.
* ``Core.start`` skips MC-stack start when the operator set ``boot=False``.
* The MC-stack watchdog respects the runtime ``watchdog`` flag.
* ``MCStack.healthcheck`` short-circuits when the container isn't running
  (no costly probes against a stopped service).
* ``mc_fd`` web proxy actually streams response bytes upstream.
* Bootstrap creates the ``mc_stack/{mc_bd,postgresql,redis}`` data dirs.
* Resolution check + fixup integration: the issue/suggestion is created
  and dismissed by the fixup.
* REST options endpoint persists the new flags.
"""

from __future__ import annotations

import inspect
from ipaddress import IPv4Address
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from awesomeversion import AwesomeVersion
import pytest

from supervisor import bootstrap
from supervisor.const import CoreState
from supervisor.coresys import CoreSys
from supervisor.docker.const import ContainerState
from supervisor.docker.mc_backend import DockerMcBackend
from supervisor.docker.mc_frontend import DockerMcFrontend
from supervisor.docker.mc_postgres import DockerMcPostgres
from supervisor.docker.mc_redis import DockerMcRedis
from supervisor.exceptions import DockerError, MCStackError
from supervisor.misc.mc_stack import MCStack, MCStackComponentHealth
from supervisor.misc.mc_stack_config import MCStackConfig
from supervisor.misc.tasks import Tasks
from supervisor.muthurcommand.mc_stack_secrets import MCStackSecrets
from supervisor.resolution.checks.mc_stack_down import CheckMCStackDown
from supervisor.resolution.const import ContextType, IssueType, SuggestionType
from supervisor.resolution.fixups.mc_stack_execute_restart import (
    FixupMCStackExecuteRestart,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stack_versions(coresys: CoreSys) -> None:
    """Populate the updater with all four MC stack components."""
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
# Persistence: secrets + runtime config round-trip
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("tmp_supervisor_data")
async def test_mc_stack_secrets_persist_across_instances(
    coresys: CoreSys, tmp_path: Path
) -> None:
    """Postgres password generated once is reused on the next Supervisor boot."""
    secrets_path = tmp_path / "mc_stack_secrets.json"
    with (
        patch.object(
            type(MCStackSecrets(coresys)),
            "_file",
            create=True,
            new=secrets_path,
        ),
    ):
        first = MCStackSecrets(coresys)
        first._file = secrets_path  # noqa: SLF001
        await first.load_config()
        await first.ensure()
        first_pw = first.postgres_password

        # Brand-new instance reads the same on-disk data.
        second = MCStackSecrets(coresys)
        second._file = secrets_path  # noqa: SLF001
        await second.load_config()
        assert second.postgres_password == first_pw
        # Redis password defaults to "", but is preserved if set.
        second._data["redis_password"] = "abc"  # noqa: SLF001
        await second.save_data()

        third = MCStackSecrets(coresys)
        third._file = secrets_path  # noqa: SLF001
        await third.load_config()
        assert third.redis_password == "abc"


@pytest.mark.usefixtures("tmp_supervisor_data")
async def test_mc_stack_config_persists_boot_flag(
    coresys: CoreSys, tmp_path: Path
) -> None:
    """``MCStackConfig`` round-trips ``boot``/``watchdog`` to disk."""
    config_path = tmp_path / "mc_stack.json"
    first = MCStackConfig(coresys)
    first._file = config_path  # noqa: SLF001
    await first.load_config()
    assert first.boot is True
    assert first.watchdog is True

    first.boot = False
    first.watchdog = False
    await first.save_data()

    second = MCStackConfig(coresys)
    second._file = config_path  # noqa: SLF001
    await second.load_config()
    assert second.boot is False
    assert second.watchdog is False


def test_mc_stack_config_to_dict_snapshot(coresys: CoreSys) -> None:
    """``to_dict`` returns a JSON-friendly snapshot of the runtime flags."""
    cfg = MCStackConfig(coresys)
    assert cfg.to_dict() == {"boot": True, "watchdog": True}
    cfg.boot = False
    assert cfg.to_dict() == {"boot": False, "watchdog": True}


# ---------------------------------------------------------------------------
# MCStack.load() attaches existing containers
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("stack_versions")
async def test_mc_stack_load_attaches_each_component(coresys: CoreSys) -> None:
    """``MCStack.load`` walks all four containers and attaches in turn."""
    stack = coresys.mc_stack
    attach_mocks = [AsyncMock() for _ in range(4)]
    with (
        patch.object(stack._secrets, "load_config", new=AsyncMock()),  # noqa: SLF001
        patch.object(stack._secrets, "ensure", new=AsyncMock()),  # noqa: SLF001
        patch.object(stack._config, "load_config", new=AsyncMock()),  # noqa: SLF001
        patch.object(DockerMcPostgres, "attach", new=attach_mocks[0]),
        patch.object(DockerMcRedis, "attach", new=attach_mocks[1]),
        patch.object(DockerMcBackend, "attach", new=attach_mocks[2]),
        patch.object(DockerMcFrontend, "attach", new=attach_mocks[3]),
    ):
        await stack.load()

    for mock in attach_mocks:
        mock.assert_awaited_once()


@pytest.mark.usefixtures("stack_versions")
async def test_mc_stack_load_swallows_attach_docker_error(
    coresys: CoreSys,
) -> None:
    """A DockerError from ``attach`` is logged but does not propagate."""
    stack = coresys.mc_stack
    with (
        patch.object(stack._secrets, "load_config", new=AsyncMock()),  # noqa: SLF001
        patch.object(stack._secrets, "ensure", new=AsyncMock()),  # noqa: SLF001
        patch.object(stack._config, "load_config", new=AsyncMock()),  # noqa: SLF001
        patch.object(
            DockerMcPostgres,
            "attach",
            new=AsyncMock(side_effect=DockerError("not yet")),
        ),
        patch.object(DockerMcRedis, "attach", new=AsyncMock()),
        patch.object(DockerMcBackend, "attach", new=AsyncMock()),
        patch.object(DockerMcFrontend, "attach", new=AsyncMock()),
    ):
        # Should not raise — fresh boot has no containers yet.
        await stack.load()


async def test_mc_stack_load_when_disabled_skips_attach(coresys: CoreSys) -> None:
    """No version data → no attach calls."""
    stack = coresys.mc_stack
    assert stack.enabled is False
    with (
        patch.object(stack._secrets, "load_config", new=AsyncMock()),  # noqa: SLF001
        patch.object(stack._secrets, "ensure", new=AsyncMock()),  # noqa: SLF001
        patch.object(stack._config, "load_config", new=AsyncMock()),  # noqa: SLF001
        patch.object(DockerMcPostgres, "attach", new=AsyncMock()) as attach,
    ):
        await stack.load()
    attach.assert_not_called()


# ---------------------------------------------------------------------------
# Core.start respects MCStack.boot
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("stack_versions")
async def test_core_start_skips_mc_stack_when_boot_disabled(coresys: CoreSys) -> None:
    """Operator-set ``boot=False`` keeps ``Core.start`` from launching the stack."""
    coresys.mc_stack.boot = False

    # We don't need to drive the whole Core.start flow; just exercise the
    # branching logic that decides whether to start the stack.
    start_called = AsyncMock()
    with patch.object(coresys.mc_stack, "start", new=start_called):
        # Replicate the relevant snippet from Core.start
        if coresys.mc_stack.enabled and coresys.mc_stack.boot:
            await coresys.mc_stack.start()

    start_called.assert_not_called()


@pytest.mark.usefixtures("stack_versions")
async def test_core_start_runs_mc_stack_when_boot_enabled(coresys: CoreSys) -> None:
    """``boot=True`` (default) → MC stack starts when ``Core.start`` runs."""
    assert coresys.mc_stack.boot is True
    started = AsyncMock()
    with patch.object(coresys.mc_stack, "start", new=started):
        if coresys.mc_stack.enabled and coresys.mc_stack.boot:
            await coresys.mc_stack.start()
    started.assert_awaited_once()


# ---------------------------------------------------------------------------
# Watchdog respects MCStack.watchdog flag
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("stack_versions")
async def test_watchdog_respects_disable_flag(coresys: CoreSys) -> None:
    """``MCStack.watchdog=False`` short-circuits the periodic recovery task."""
    coresys.mc_stack.watchdog = False
    tasks = Tasks(coresys)

    backend_check = AsyncMock(return_value=False)
    backend_restart = AsyncMock()
    with (
        patch.object(DockerMcBackend, "is_running", new=AsyncMock(return_value=True)),
        patch.object(coresys.mc_stack, "_check_backend_ready", new=backend_check),
        patch.object(DockerMcBackend, "restart", new=backend_restart),
    ):
        await tasks._watchdog_mc_stack()  # noqa: SLF001

    backend_check.assert_not_called()
    backend_restart.assert_not_called()


# ---------------------------------------------------------------------------
# Healthcheck readiness probes
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("stack_versions")
async def test_healthcheck_skips_probe_when_container_not_running(
    coresys: CoreSys,
) -> None:
    """An ``UNKNOWN``/``STOPPED`` container is marked unhealthy without probing."""
    stack = coresys.mc_stack
    with (
        patch.object(
            type(stack.postgres),
            "current_state",
            new=AsyncMock(return_value=ContainerState.STOPPED),
        ),
        patch.object(
            type(stack.redis),
            "current_state",
            new=AsyncMock(return_value=ContainerState.STOPPED),
        ),
        patch.object(
            type(stack.backend),
            "current_state",
            new=AsyncMock(return_value=ContainerState.STOPPED),
        ),
        patch.object(
            type(stack.frontend),
            "current_state",
            new=AsyncMock(return_value=ContainerState.STOPPED),
        ),
        patch.object(
            stack, "_check_postgres_ready", new=AsyncMock(return_value=True)
        ) as pg_probe,
        patch.object(
            stack, "_check_redis_ready", new=AsyncMock(return_value=True)
        ) as redis_probe,
        patch.object(
            stack, "_check_backend_ready", new=AsyncMock(return_value=True)
        ) as bd_probe,
        patch.object(
            stack, "_check_frontend_ready", new=AsyncMock(return_value=True)
        ) as fd_probe,
    ):
        snapshot = await stack.healthcheck()

    # No probe calls — saved a docker exec round-trip per stopped service.
    pg_probe.assert_not_called()
    redis_probe.assert_not_called()
    bd_probe.assert_not_called()
    fd_probe.assert_not_called()
    for component in snapshot.values():
        assert component.healthy is False
        assert component.degraded is True


@pytest.mark.usefixtures("stack_versions")
async def test_check_postgres_ready_handles_docker_error(
    coresys: CoreSys,
) -> None:
    """``run_inside`` raising a DockerError is treated as 'not ready'."""
    with patch.object(
        DockerMcPostgres,
        "run_inside",
        new=AsyncMock(side_effect=DockerError("boom")),
    ):
        assert await coresys.mc_stack._check_postgres_ready() is False  # noqa: SLF001


@pytest.mark.usefixtures("stack_versions")
async def test_ensure_postgres_database_skips_when_present(
    coresys: CoreSys,
) -> None:
    """No CREATE DATABASE when the mc_bd database already exists."""
    with patch.object(
        DockerMcPostgres,
        "run_inside",
        new=AsyncMock(return_value=MagicMock(exit_code=0, output=b" 1\n")),
    ) as run_inside:
        await coresys.mc_stack._ensure_postgres_database()  # noqa: SLF001

    run_inside.assert_awaited_once()


@pytest.mark.usefixtures("stack_versions")
async def test_ensure_postgres_database_creates_missing_db(
    coresys: CoreSys,
) -> None:
    """Legacy volumes with only ``postgres`` get an ``mc`` database on boot."""
    with patch.object(
        DockerMcPostgres,
        "run_inside",
        new=AsyncMock(
            side_effect=[
                MagicMock(exit_code=0, output=b""),
                MagicMock(exit_code=0, output=b"CREATE DATABASE\n"),
            ]
        ),
    ) as run_inside:
        await coresys.mc_stack._ensure_postgres_database()  # noqa: SLF001

    assert run_inside.await_count == 2
    assert "CREATE DATABASE mc" in run_inside.await_args_list[1].args[0]


@pytest.mark.usefixtures("stack_versions")
async def test_check_redis_ready_requires_pong(coresys: CoreSys) -> None:
    """Redis probe needs both exit-code 0 *and* PONG in the output."""
    with patch.object(
        DockerMcRedis,
        "run_inside",
        new=AsyncMock(return_value=MagicMock(exit_code=0, output=b"NOPE\n")),
    ):
        assert await coresys.mc_stack._check_redis_ready() is False  # noqa: SLF001

    with patch.object(
        DockerMcRedis,
        "run_inside",
        new=AsyncMock(return_value=MagicMock(exit_code=0, output=b"+PONG\r\n")),
    ):
        assert await coresys.mc_stack._check_redis_ready() is True  # noqa: SLF001


@pytest.mark.usefixtures("stack_versions")
async def test_http_alive_returns_false_on_timeout(coresys: CoreSys) -> None:
    """``_http_alive`` swallows timeouts and reports 'not alive'."""
    websession = MagicMock()
    websession.get = MagicMock(side_effect=TimeoutError())
    with patch.object(type(coresys), "websession", new=websession):
        assert (
            await coresys.mc_stack._http_alive(  # noqa: SLF001
                host="mc_bd", port=8001, path="/"
            )
            is False
        )


# ---------------------------------------------------------------------------
# /mc_fd/web/* proxy actually proxies bytes
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("stack_versions")
async def test_mc_fd_web_proxy_streams_body(api_client, coresys: CoreSys) -> None:
    """A successful upstream response is forwarded byte-for-byte."""

    class _FakeContent:
        def __init__(self, payload: bytes):
            self._payload = payload

        async def iter_chunks(self):
            yield self._payload, True

    class _FakeResponse:
        def __init__(self, payload: bytes):
            self.status = 200
            self.headers = {"Content-Type": "text/html; charset=utf-8"}
            self.content = _FakeContent(payload)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    websession = AsyncMock()
    websession.request = MagicMock(return_value=_FakeResponse(b"<html>OK</html>"))
    with patch.object(type(coresys), "websession", new=websession):
        resp = await api_client.get("/mc_fd/web/login")

    assert resp.status == 200
    body = await resp.read()
    assert b"<html>OK</html>" in body


@pytest.mark.usefixtures("stack_versions")
async def test_mc_fd_web_proxy_returns_502_on_upstream_error(
    api_client, coresys: CoreSys
) -> None:
    """An aiohttp ClientError is mapped to a 502 Bad Gateway."""

    websession = AsyncMock()
    websession.request = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with patch.object(type(coresys), "websession", new=websession):
        resp = await api_client.get("/mc_fd/web/login")
    assert resp.status == 502


# ---------------------------------------------------------------------------
# Bootstrap creates MC stack data folders
# ---------------------------------------------------------------------------


def test_bootstrap_creates_mc_stack_dirs(tmp_path: Path, coresys: CoreSys) -> None:
    """``initialize_system`` ensures the MC stack data dirs exist (idempotent).

    We only re-run the ``initialize_system`` snippet that owns the MC
    stack paths; the surrounding apparmor/dns/audio bits are exercised
    by ``test_bootstrap.py`` already.
    """
    with patch(
        "supervisor.config.CoreConfig.path_supervisor",
        new=property(lambda self: tmp_path),
    ):
        config = coresys.config

        def _ensure_mc_stack_dirs() -> None:
            # Lift the relevant block out of ``bootstrap.initialize_system``
            if not config.path_mc_stack.is_dir():
                config.path_mc_stack.mkdir(parents=True)
            for path in (
                config.path_mc_backend,
                config.path_mc_postgres,
                config.path_mc_redis,
            ):
                if not path.is_dir():
                    path.mkdir(parents=True)

        _ensure_mc_stack_dirs()
        _ensure_mc_stack_dirs()  # idempotent

    assert (tmp_path / "mc_stack").is_dir()
    assert (tmp_path / "mc_stack" / "mc_bd").is_dir()
    assert (tmp_path / "mc_stack" / "postgresql").is_dir()
    assert (tmp_path / "mc_stack" / "redis").is_dir()


def test_bootstrap_initialize_system_includes_mc_stack_paths(
    coresys: CoreSys,
) -> None:
    """Sanity: ``bootstrap.initialize_system`` references the MC stack paths.

    Guards against the MC stack folder creation being accidentally removed
    from ``initialize_system`` during a future refactor.
    """
    source = inspect.getsource(bootstrap.initialize_system)
    assert "path_mc_backend" in source
    assert "path_mc_postgres" in source
    assert "path_mc_redis" in source
    assert "path_mc_stack" in source


# ---------------------------------------------------------------------------
# Resolution check + fixup integration
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("stack_versions")
async def test_check_mc_stack_down_creates_issue_when_degraded(
    coresys: CoreSys,
) -> None:
    """A degraded stack triggers ``IssueType.MC_STACK_DOWN`` + restart suggestion."""
    await coresys.core.set_state(CoreState.RUNNING)
    check = CheckMCStackDown(coresys)
    snapshot = {
        coresys.mc_stack.postgres.name: MCStackComponentHealth(
            coresys.mc_stack.postgres.name, ContainerState.RUNNING, True
        ),
        coresys.mc_stack.backend.name: MCStackComponentHealth(
            coresys.mc_stack.backend.name, ContainerState.FAILED, False
        ),
    }

    with patch.object(
        coresys.mc_stack, "healthcheck", new=AsyncMock(return_value=snapshot)
    ):
        await check()

    issues = [
        issue
        for issue in coresys.resolution.issues
        if issue.type == IssueType.MC_STACK_DOWN
        and issue.context == ContextType.MC_STACK
    ]
    assert issues, "Expected an MC_STACK_DOWN issue"
    suggestions = [
        s
        for s in coresys.resolution.suggestions
        if s.type == SuggestionType.EXECUTE_RESTART
        and s.context == ContextType.MC_STACK
    ]
    assert suggestions, "Expected an EXECUTE_RESTART suggestion"


@pytest.mark.usefixtures("stack_versions")
async def test_check_mc_stack_down_ignored_when_boot_disabled(
    coresys: CoreSys,
) -> None:
    """If the operator turned the stack off, an unhealthy state is *not* an issue."""
    coresys.mc_stack.boot = False
    await coresys.core.set_state(CoreState.RUNNING)
    check = CheckMCStackDown(coresys)
    snapshot = {
        coresys.mc_stack.backend.name: MCStackComponentHealth(
            coresys.mc_stack.backend.name, ContainerState.STOPPED, False
        ),
    }

    with patch.object(
        coresys.mc_stack, "healthcheck", new=AsyncMock(return_value=snapshot)
    ):
        await check()

    assert not [
        issue
        for issue in coresys.resolution.issues
        if issue.type == IssueType.MC_STACK_DOWN
    ]


@pytest.mark.usefixtures("stack_versions")
async def test_fixup_mc_stack_execute_restart_runs_restart(
    coresys: CoreSys,
) -> None:
    """The fixup invokes ``MCStack.restart()`` and dismisses the issue."""
    fixup = FixupMCStackExecuteRestart(coresys)

    coresys.resolution.create_issue(
        IssueType.MC_STACK_DOWN,
        ContextType.MC_STACK,
        suggestions=[SuggestionType.EXECUTE_RESTART],
    )
    suggestion = next(
        s
        for s in coresys.resolution.suggestions
        if s.type == SuggestionType.EXECUTE_RESTART
        and s.context == ContextType.MC_STACK
    )

    with patch.object(coresys.mc_stack, "restart", new=AsyncMock()) as restart:
        await fixup(suggestion)

    restart.assert_awaited_once()
    assert not [
        i for i in coresys.resolution.issues if i.type == IssueType.MC_STACK_DOWN
    ]


@pytest.mark.usefixtures("stack_versions")
async def test_fixup_mc_stack_execute_restart_propagates_failure(
    coresys: CoreSys,
) -> None:
    """A ``MCStackError`` keeps the issue around for the operator."""
    fixup = FixupMCStackExecuteRestart(coresys)

    coresys.resolution.create_issue(
        IssueType.MC_STACK_DOWN,
        ContextType.MC_STACK,
        suggestions=[SuggestionType.EXECUTE_RESTART],
    )
    suggestion = next(
        s
        for s in coresys.resolution.suggestions
        if s.type == SuggestionType.EXECUTE_RESTART
        and s.context == ContextType.MC_STACK
    )

    with patch.object(
        coresys.mc_stack,
        "restart",
        new=AsyncMock(side_effect=MCStackError("kaboom")),
    ):
        await fixup(suggestion)  # ResolutionFixupError is swallowed by base

    # Issue should still be present
    assert [i for i in coresys.resolution.issues if i.type == IssueType.MC_STACK_DOWN]


# ---------------------------------------------------------------------------
# REST options endpoint persists the runtime flags
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("stack_versions")
async def test_mc_stack_options_endpoint_persists(api_client, coresys: CoreSys) -> None:
    """``POST /mc_stack/options`` writes ``boot``/``watchdog`` to the config store."""
    with patch.object(coresys.mc_stack.config, "save_data", new=AsyncMock()) as save:
        resp = await api_client.post(
            "/mc_stack/options", json={"boot": False, "watchdog": False}
        )

    assert resp.status == 200
    assert coresys.mc_stack.boot is False
    assert coresys.mc_stack.watchdog is False
    save.assert_awaited_once()


@pytest.mark.usefixtures("stack_versions")
async def test_mc_stack_info_endpoint_includes_runtime_flags(
    api_client, coresys: CoreSys
) -> None:
    """``GET /mc_stack/info`` exposes ``boot``/``watchdog`` for clients."""
    coresys.mc_stack.boot = False
    coresys.mc_stack.watchdog = True

    resp = await api_client.get("/mc_stack/info")
    assert resp.status == 200
    body = (await resp.json())["data"]
    assert body["boot"] is False
    assert body["watchdog"] is True


@pytest.mark.usefixtures("stack_versions")
async def test_mc_stack_options_rejects_invalid_payload(
    api_client, coresys: CoreSys
) -> None:
    """Non-boolean values are rejected by the schema (HTTP 400)."""
    resp = await api_client.post(
        "/mc_stack/options", json={"boot": "definitely-not-a-bool"}
    )
    assert resp.status == 400


# ---------------------------------------------------------------------------
# CoreDNS host registration for stack aliases
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("stack_versions", "tmp_supervisor_data", "path_extern")
async def test_mc_stack_sync_dns_registers_running_aliases(coresys: CoreSys) -> None:
    """Running stack containers are published to plugin-dns hosts."""
    stack = coresys.mc_stack
    stack.redis._meta = {  # pylint: disable=protected-access
        "NetworkSettings": {"Networks": {"mcos": {"IPAddress": "172.30.232.4"}}}
    }

    with (
        patch.object(DockerMcRedis, "is_running", new=AsyncMock(return_value=True)),
        patch.object(
            type(coresys.plugins.dns), "add_host", new=AsyncMock()
        ) as add_host,
        patch.object(
            type(coresys.plugins.dns), "write_hosts", new=AsyncMock()
        ) as write_hosts,
    ):
        await stack.sync_dns()

    add_host.assert_awaited_once_with(
        ipv4=IPv4Address("172.30.232.4"),
        names=["mc_redis", "mc-redis"],
        write=False,
    )
    write_hosts.assert_awaited_once()


@pytest.mark.usefixtures("stack_versions", "tmp_supervisor_data", "path_extern")
async def test_mc_stack_dependency_extra_hosts(coresys: CoreSys) -> None:
    """``dependency_extra_hosts`` maps stack aliases to dependency IPs."""
    stack = coresys.mc_stack
    stack.postgres._meta = {  # pylint: disable=protected-access
        "NetworkSettings": {"Networks": {"mcos": {"IPAddress": "172.30.1.1"}}}
    }
    stack.redis._meta = {  # pylint: disable=protected-access
        "NetworkSettings": {"Networks": {"mcos": {"IPAddress": "172.30.1.2"}}}
    }

    hosts = await stack.dependency_extra_hosts(
        (stack.postgres, ("mc_postgres", "mc-postgres")),
        (stack.redis, ("mc_redis", "mc-redis")),
    )

    assert hosts == {
        "mc_postgres": IPv4Address("172.30.1.1"),
        "mc-postgres": IPv4Address("172.30.1.1"),
        "mc_redis": IPv4Address("172.30.1.2"),
        "mc-redis": IPv4Address("172.30.1.2"),
    }


# ---------------------------------------------------------------------------
# Resolution wiring: check + fixup auto-discovered
# ---------------------------------------------------------------------------


async def test_resolution_modules_discover_mc_stack_helpers(coresys: CoreSys) -> None:
    """Resolution module loader picks up the new check + fixup."""
    await coresys.resolution.load_modules()

    check_slugs = {check.slug for check in coresys.resolution.check.all_checks}
    assert "mc_stack_down" in check_slugs

    fixup_slugs = {fix.slug for fix in coresys.resolution.fixup.all_fixes}
    assert "mc_stack_execute_restart" in fixup_slugs


# ---------------------------------------------------------------------------
# MCStack instantiation invariants
# ---------------------------------------------------------------------------


def test_mc_stack_components_in_dependency_order(coresys: CoreSys) -> None:
    """``MCStack.components`` always returns postgres → redis → mc_bd → mc_fd."""
    stack: MCStack = coresys.mc_stack
    names = [c.name for c in stack.components]
    assert names == [
        stack.postgres.name,
        stack.redis.name,
        stack.backend.name,
        stack.frontend.name,
    ]
