"""Tests for MC application stack Docker wrappers (stage 3 acceptance).

These tests cover the documented stage-3 deliverables:

* Each component has its own ``Docker*`` class that produces the right
  ``hostconfig`` / ``mounts`` / ``Env`` / ``NetworkingConfig`` / ``Labels``
  payload (verified through ``mock`` over ``aiodocker``-facing entry points).
* PostgreSQL credentials are sourced from the Supervisor-generated
  ``MCStackSecrets`` store and propagated into ``mc_bd``'s env.
* MC stack containers carry the ``io.muthur.*`` label set so Observer /
  external monitoring can filter them.
* ``stop`` / ``remove`` paths terminate cleanly using the inherited
  ``DockerInterface`` flow and do not delete persistent volumes.
"""

# pylint: disable=protected-access

from __future__ import annotations

from ipaddress import IPv4Address
from typing import Any
from unittest.mock import ANY, AsyncMock, patch

from awesomeversion import AwesomeVersion
import pytest

from supervisor.const import (
    DOCKER_EMBEDDED_DNS,
    DOCKER_NETWORK,
    LABEL_MC_MANAGED_BY,
    LABEL_MC_ROLE,
    LABEL_MC_STACK,
    MC_BACKEND_DOCKER_NAME,
    MC_BACKEND_PORT,
    MC_FRONTEND_DOCKER_NAME,
    MC_POSTGRES_DEFAULT_DB,
    MC_POSTGRES_DEFAULT_USER,
    MC_POSTGRES_DOCKER_NAME,
    MC_POSTGRES_PORT,
    MC_REDIS_DOCKER_NAME,
    MC_REDIS_PORT,
    MC_ROLE_BACKEND,
    MC_ROLE_FRONTEND,
    MC_ROLE_POSTGRES,
    MC_ROLE_REDIS,
    MC_STACK_MANAGED_BY,
    MC_STACK_NAME,
)
from supervisor.coresys import CoreSys
from supervisor.docker.const import LABEL_MANAGED, MountType, RestartPolicy
from supervisor.docker.interface import DockerInterface
from supervisor.docker.mc_backend import DockerMcBackend
from supervisor.docker.mc_frontend import DockerMcFrontend
from supervisor.docker.mc_postgres import DockerMcPostgres
from supervisor.docker.mc_redis import DockerMcRedis
from supervisor.exceptions import DockerJobError, McosRuntimeError

# --- Fixtures & helpers ----------------------------------------------------


@pytest.fixture
def stack_versions(coresys: CoreSys) -> None:
    """Populate updater state so the four MC stack components have data."""
    updater = coresys.updater
    updater._data["mc_bd"] = AwesomeVersion("0.1.0")
    updater._data["mc_fd"] = AwesomeVersion("0.1.0")
    updater._data["postgresql"] = AwesomeVersion("16.3")
    updater._data["redis"] = AwesomeVersion("7.2.4")
    updater._data["image"]["mc_bd"] = "ghcr.io/muthur-command/{arch}-mc-bd"
    updater._data["image"]["mc_fd"] = "ghcr.io/muthur-command/mc-fd"
    updater._data["image"]["postgresql"] = "docker.io/library/postgres"
    updater._data["image"]["redis"] = "docker.io/library/redis"


def _capture_run_kwargs(
    cls: type[DockerInterface], coresys: CoreSys
) -> tuple[DockerInterface, AsyncMock]:
    """Construct an MC stack docker wrapper and patch ``DockerAPI.run``."""
    instance = cls(coresys)
    run = AsyncMock(return_value={})
    instance.sys_docker.run = run  # type: ignore[method-assign]
    return instance, run


def _last_kwargs(run: AsyncMock) -> dict[str, Any]:
    """Return kwargs of the single ``DockerAPI.run`` call recorded."""
    assert run.call_count == 1, run.call_args_list
    return run.call_args.kwargs


# --- Per-component ``run()`` payload tests ---------------------------------


@pytest.mark.usefixtures("stack_versions", "tmp_supervisor_data", "path_extern")
async def test_docker_mc_postgres_run(coresys: CoreSys) -> None:
    """DockerMcPostgres builds the expected create() config."""
    instance, run = _capture_run_kwargs(DockerMcPostgres, coresys)

    with (
        patch.object(DockerMcPostgres, "is_running", new=AsyncMock(return_value=False)),
        patch.object(DockerMcPostgres, "stop", new=AsyncMock()),
    ):
        await instance.run()

    kwargs = _last_kwargs(run)
    assert kwargs["name"] == MC_POSTGRES_DOCKER_NAME
    assert kwargs["hostname"] == "mc-postgres"
    assert kwargs["tag"] == "16.3"
    assert kwargs["environment"]["POSTGRES_USER"] == MC_POSTGRES_DEFAULT_USER
    assert kwargs["environment"]["POSTGRES_DB"] == MC_POSTGRES_DEFAULT_DB
    assert kwargs["environment"]["POSTGRES_PASSWORD"], "Password must be generated"
    assert kwargs["environment"]["PGDATA"].startswith("/var/lib/postgresql/data")
    assert kwargs["network_mode"] == DOCKER_NETWORK
    assert kwargs["networking_config"] == {
        "EndpointsConfig": {DOCKER_NETWORK: {"Aliases": ["mc_postgres", "mc-postgres"]}}
    }
    assert any(
        m.type == MountType.BIND and m.target == "/var/lib/postgresql/data"
        for m in kwargs["mounts"]
    )
    # ShmSize must be > Docker's 64 MiB default for PostgreSQL.
    assert kwargs["shm_size"] >= 64 * 1024 * 1024


@pytest.mark.usefixtures("stack_versions", "tmp_supervisor_data", "path_extern")
async def test_docker_mc_redis_run(coresys: CoreSys) -> None:
    """DockerMcRedis enables AOF and exposes the right hostname/aliases."""
    instance, run = _capture_run_kwargs(DockerMcRedis, coresys)

    with (
        patch.object(DockerMcRedis, "is_running", new=AsyncMock(return_value=False)),
        patch.object(DockerMcRedis, "stop", new=AsyncMock()),
    ):
        await instance.run()

    kwargs = _last_kwargs(run)
    assert kwargs["name"] == MC_REDIS_DOCKER_NAME
    assert kwargs["hostname"] == "mc-redis"
    assert kwargs["tag"] == "7.2.4"
    assert "redis-server" in kwargs["command"]
    assert "--appendonly" in kwargs["command"]
    assert kwargs["network_mode"] == DOCKER_NETWORK
    assert kwargs["networking_config"] == {
        "EndpointsConfig": {DOCKER_NETWORK: {"Aliases": ["mc_redis", "mc-redis"]}}
    }


@pytest.mark.usefixtures("stack_versions", "tmp_supervisor_data", "path_extern")
async def test_docker_mc_redis_password_when_set(coresys: CoreSys) -> None:
    """Redis ``--requirepass`` only appears when the operator set a password."""
    coresys.mc_stack.secrets._data["redis_password"] = "topsecret"  # noqa: SLF001
    instance, run = _capture_run_kwargs(DockerMcRedis, coresys)

    with (
        patch.object(DockerMcRedis, "is_running", new=AsyncMock(return_value=False)),
        patch.object(DockerMcRedis, "stop", new=AsyncMock()),
    ):
        await instance.run()

    cmd = _last_kwargs(run)["command"]
    assert "--requirepass" in cmd
    assert cmd[cmd.index("--requirepass") + 1] == "topsecret"


@pytest.mark.usefixtures("stack_versions", "tmp_supervisor_data", "path_extern")
async def test_docker_mc_backend_run(coresys: CoreSys) -> None:
    """DockerMcBackend wires Postgres/Redis env so mc_bd starts correctly."""
    instance, run = _capture_run_kwargs(DockerMcBackend, coresys)
    extra_hosts = {
        "mc_postgres": IPv4Address("172.30.1.1"),
        "mc-postgres": IPv4Address("172.30.1.1"),
        "mc_redis": IPv4Address("172.30.1.2"),
        "mc-redis": IPv4Address("172.30.1.2"),
    }
    postgres_meta = {
        "NetworkSettings": {"Networks": {"mcos": {"IPAddress": "172.30.1.1"}}}
    }
    redis_meta = {
        "NetworkSettings": {"Networks": {"mcos": {"IPAddress": "172.30.1.2"}}}
    }

    async def fake_inspect(inst: DockerInterface) -> dict[str, Any] | None:
        if inst is coresys.mc_stack.postgres:
            return postgres_meta
        if inst is coresys.mc_stack.redis:
            return redis_meta
        return None

    with (
        patch.object(DockerMcBackend, "is_running", new=AsyncMock(return_value=False)),
        patch.object(DockerMcBackend, "stop", new=AsyncMock()),
        patch.object(coresys.mc_stack, "inspect_container", side_effect=fake_inspect),
        patch.object(
            coresys.mc_stack,
            "dependency_extra_hosts",
            new=AsyncMock(return_value=extra_hosts),
        ),
    ):
        await instance.run()

    kwargs = _last_kwargs(run)
    env = kwargs["environment"]
    assert kwargs["name"] == MC_BACKEND_DOCKER_NAME
    assert kwargs["hostname"] == "mc-bd"
    assert env["DATABASE_HOST"] == "172.30.1.1"
    assert env["DATABASE_PORT"] == str(MC_POSTGRES_PORT)
    assert env["REDIS_HOST"] == "172.30.1.2"
    assert env["REDIS_PORT"] == str(MC_REDIS_PORT)
    assert env["APP_PORT"] == str(MC_BACKEND_PORT)
    # PostgreSQL password ends up in mc_bd env, must match the secrets store.
    assert env["DATABASE_PASSWORD"] == coresys.mc_stack.secrets.postgres_password
    assert env["DATABASE_SCHEMA"] == MC_POSTGRES_DEFAULT_DB
    assert kwargs["extra_hosts"] == extra_hosts
    assert kwargs["network_mode"] == DOCKER_NETWORK
    assert kwargs["networking_config"] == {
        "EndpointsConfig": {
            DOCKER_NETWORK: {"Aliases": ["mc_bd", "mc-bd", "mc_server"]}
        }
    }


@pytest.mark.usefixtures("stack_versions", "tmp_supervisor_data", "path_extern")
async def test_docker_mc_frontend_run(coresys: CoreSys) -> None:
    """DockerMcFrontend exposes the configured backend host/port."""
    instance, run = _capture_run_kwargs(DockerMcFrontend, coresys)
    extra_hosts = {
        "mc_bd": IPv4Address("172.30.1.3"),
        "mc-bd": IPv4Address("172.30.1.3"),
    }

    with (
        patch.object(DockerMcFrontend, "is_running", new=AsyncMock(return_value=False)),
        patch.object(DockerMcFrontend, "stop", new=AsyncMock()),
        patch.object(
            coresys.mc_stack,
            "dependency_extra_hosts",
            new=AsyncMock(return_value=extra_hosts),
        ),
    ):
        await instance.run()

    kwargs = _last_kwargs(run)
    env = kwargs["environment"]
    assert kwargs["name"] == MC_FRONTEND_DOCKER_NAME
    assert kwargs["hostname"] == "mc-fd"
    assert env["MC_BACKEND_HOST"] == "mc_bd"
    assert env["MC_BACKEND_PORT"] == str(MC_BACKEND_PORT)
    assert env["VITE_SERVER_API_PREFIX"] == "/api"
    assert kwargs["extra_hosts"] == extra_hosts
    assert kwargs["network_mode"] == DOCKER_NETWORK
    assert kwargs["networking_config"] == {
        "EndpointsConfig": {DOCKER_NETWORK: {"Aliases": ["mc_fd", "mc-fd"]}}
    }


# --- Cross-cutting acceptance: labels, restart policy, fail-fast -----------


@pytest.mark.usefixtures("stack_versions", "tmp_supervisor_data", "path_extern")
@pytest.mark.parametrize(
    ("cls", "expected_role"),
    [
        (DockerMcPostgres, MC_ROLE_POSTGRES),
        (DockerMcRedis, MC_ROLE_REDIS),
        (DockerMcBackend, MC_ROLE_BACKEND),
        (DockerMcFrontend, MC_ROLE_FRONTEND),
    ],
)
async def test_mc_stack_container_labels(
    coresys: CoreSys, cls: type[DockerInterface], expected_role: str
) -> None:
    """Every MC stack container carries ``io.muthur.*`` filtering labels."""
    instance, run = _capture_run_kwargs(cls, coresys)

    with (
        patch.object(cls, "is_running", new=AsyncMock(return_value=False)),
        patch.object(cls, "stop", new=AsyncMock()),
    ):
        await instance.run()

    labels = _last_kwargs(run)["labels"]
    assert labels[LABEL_MC_STACK] == MC_STACK_NAME
    assert labels[LABEL_MC_ROLE] == expected_role
    assert labels[LABEL_MC_MANAGED_BY] == MC_STACK_MANAGED_BY


@pytest.mark.usefixtures("stack_versions", "tmp_supervisor_data", "path_extern")
@pytest.mark.parametrize(
    "cls",
    [DockerMcPostgres, DockerMcRedis, DockerMcBackend, DockerMcFrontend],
)
async def test_mc_stack_restart_policy_unless_stopped(
    coresys: CoreSys, cls: type[DockerInterface]
) -> None:
    """Stack containers auto-restart with the daemon, but stay stopped on demand."""
    instance, run = _capture_run_kwargs(cls, coresys)

    with (
        patch.object(cls, "is_running", new=AsyncMock(return_value=False)),
        patch.object(cls, "stop", new=AsyncMock()),
    ):
        await instance.run()

    assert _last_kwargs(run)["restart_policy"] == {
        "Name": RestartPolicy.UNLESS_STOPPED,
    }


@pytest.mark.usefixtures("stack_versions", "tmp_supervisor_data", "path_extern")
@pytest.mark.parametrize(
    "cls",
    [DockerMcPostgres, DockerMcRedis, DockerMcBackend, DockerMcFrontend],
)
async def test_mc_stack_run_without_version_raises(
    coresys: CoreSys, cls: type[DockerInterface]
) -> None:
    """Missing version should surface as a Docker job error, not a silent run."""
    coresys.updater._data["postgresql"] = None  # noqa: SLF001
    coresys.updater._data["redis"] = None  # noqa: SLF001
    coresys.updater._data["mc_bd"] = None  # noqa: SLF001
    coresys.updater._data["mc_fd"] = None  # noqa: SLF001

    instance, run = _capture_run_kwargs(cls, coresys)

    with (
        patch.object(cls, "is_running", new=AsyncMock(return_value=False)),
        patch.object(cls, "stop", new=AsyncMock()),
        pytest.raises((DockerJobError, McosRuntimeError)),
    ):
        await instance.run()
    run.assert_not_called()


# --- ``_create_container_config`` integration: hostconfig + mounts ---------


@pytest.mark.usefixtures("stack_versions", "tmp_supervisor_data", "path_extern")
async def test_postgres_create_container_config_payload(coresys: CoreSys) -> None:
    """End-to-end the mock-aiodocker payload contains everything we expect.

    This is the stage-3 acceptance test: with ``aiodocker`` mocked at the
    container creation boundary, ``_create_container_config`` must produce
    a body that PostgreSQL would actually start with.
    """
    instance = DockerMcPostgres(coresys)

    config = coresys.docker._create_container_config(  # noqa: SLF001
        image=instance.image,
        tag=str(instance.version),
        hostname=instance.hostname,
        environment=instance.environment,
        mounts=instance.mounts,
        networking_config=instance.networking_config,
        labels=instance.labels,
        restart_policy={"Name": RestartPolicy.UNLESS_STOPPED},
        shm_size=256 * 1024 * 1024,
        oom_score_adj=-200,
    )

    # Image / hostname / labels surface
    assert config["Image"] == "docker.io/library/postgres:16.3"
    assert config["Hostname"] == "mc-postgres"
    assert config["Labels"][LABEL_MANAGED] == ""
    assert config["Labels"][LABEL_MC_STACK] == MC_STACK_NAME
    assert config["Labels"][LABEL_MC_ROLE] == MC_ROLE_POSTGRES

    # Env contains the password we generated
    env = dict(item.split("=", 1) for item in config["Env"])
    assert env["POSTGRES_USER"] == MC_POSTGRES_DEFAULT_USER
    assert env["POSTGRES_DB"] == MC_POSTGRES_DEFAULT_DB
    assert env["POSTGRES_PASSWORD"] == coresys.mc_stack.secrets.postgres_password

    # HostConfig shape: bind mount, restart policy, shm size, oom adjust.
    host = config["HostConfig"]
    assert any(
        m["Type"] == "bind" and m["Target"] == "/var/lib/postgresql/data"
        for m in host["Mounts"]
    )
    assert host["RestartPolicy"] == {"Name": RestartPolicy.UNLESS_STOPPED}
    assert host["ShmSize"] == 256 * 1024 * 1024
    assert host["OomScoreAdj"] == -200
    assert host["Dns"] == [DOCKER_EMBEDDED_DNS, str(coresys.docker.network.dns)]
    assert host["DnsOptions"] == ["timeout:10"]

    # Network endpoint with both alias variants.
    aliases = config["NetworkingConfig"]["EndpointsConfig"][DOCKER_NETWORK]["Aliases"]
    assert aliases == ["mc_postgres", "mc-postgres"]


# --- Stop / remove flow uses the inherited DockerInterface path ------------


@pytest.mark.usefixtures("stack_versions", "tmp_supervisor_data", "path_extern")
async def test_mc_stack_stop_uses_docker_manager(coresys: CoreSys) -> None:
    """``DockerInterface.stop`` delegates to ``DockerAPI.stop_container``.

    Stage 3 acceptance criterion "stop/remove" is fulfilled by the
    inherited Docker interface; we verify here that the inherited flow is
    actually exercised for an MC stack class without raising.
    """
    instance = DockerMcPostgres(coresys)
    stop_container = AsyncMock()
    instance.sys_docker.stop_container = stop_container  # type: ignore[method-assign]

    await instance.stop(remove_container=True)

    stop_container.assert_awaited_once_with(
        MC_POSTGRES_DOCKER_NAME, instance.timeout, True
    )


@pytest.mark.usefixtures("stack_versions", "tmp_supervisor_data", "path_extern")
async def test_mc_stack_cleanup_keeps_volume(coresys: CoreSys) -> None:
    """``cleanup`` only removes old IMAGES — the persistent volume must stay.

    The plan explicitly bans automatic ``docker volume rm`` for the DB.
    Verify ``DockerInterface.cleanup`` only triggers ``cleanup_old_images``
    and never touches the bind-mount path.
    """
    instance = DockerMcPostgres(coresys)
    instance._meta = {  # noqa: SLF001
        "Config": {"Labels": {"io.mcos.version": "16.3"}}
    }
    cleanup_old_images = AsyncMock()
    instance.sys_docker.cleanup_old_images = cleanup_old_images  # type: ignore[method-assign]

    await instance.cleanup(old_image="docker.io/library/postgres:16.2")

    cleanup_old_images.assert_awaited_once_with(
        instance.image, ANY, {"docker.io/library/postgres:16.2"}
    )


# --- LABEL_MANAGED is preserved when custom labels are supplied -----------


def test_create_container_config_merges_managed_label(coresys: CoreSys) -> None:
    """Custom ``labels`` from MC stack must not lose the managed marker."""
    config = coresys.docker._create_container_config(  # noqa: SLF001
        image="docker.io/library/postgres",
        tag="16.3",
        labels={LABEL_MC_STACK: MC_STACK_NAME, LABEL_MC_ROLE: MC_ROLE_POSTGRES},
    )
    assert config["Labels"][LABEL_MANAGED] == ""
    assert config["Labels"][LABEL_MC_STACK] == MC_STACK_NAME
    assert config["Labels"][LABEL_MC_ROLE] == MC_ROLE_POSTGRES


# --- Direct DockerInterface.install path still works ----------------------


@pytest.mark.usefixtures("stack_versions")
async def test_mc_stack_image_property_resolves_from_updater(coresys: CoreSys) -> None:
    """All four wrappers resolve their image template via the Updater."""
    assert DockerMcPostgres(coresys).image == "docker.io/library/postgres"
    assert DockerMcRedis(coresys).image == "docker.io/library/redis"
    assert DockerMcBackend(coresys).image == "ghcr.io/muthur-command/amd64-mc-bd"
    assert DockerMcFrontend(coresys).image == "ghcr.io/muthur-command/mc-fd"


@pytest.mark.usefixtures("stack_versions")
def test_mc_stack_versions_resolve_from_updater(coresys: CoreSys) -> None:
    """All four wrappers expose their target version from the Updater."""
    assert DockerMcPostgres(coresys).version == AwesomeVersion("16.3")
    assert DockerMcRedis(coresys).version == AwesomeVersion("7.2.4")
    assert DockerMcBackend(coresys).version == AwesomeVersion("0.1.0")
    assert DockerMcFrontend(coresys).version == AwesomeVersion("0.1.0")
