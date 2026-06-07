"""Docker wrapper for the MC stack backend (mc_bd) container.

The ``mc_bd`` API container runs FastAPI/Granian and depends on PostgreSQL +
Redis being reachable inside the ``mcos`` network. It is exposed under the
alias ``mc_bd`` (matches the upstream nginx ``proxy_pass`` host used by
``mc_fd``) and exposes :data:`MC_BACKEND_PORT` for in-network HTTP calls.
"""

from __future__ import annotations

import logging
from typing import Final

from awesomeversion import AwesomeVersion

from ..const import (
    MC_BACKEND_DOCKER_NAME,
    MC_BACKEND_PORT,
    MC_POSTGRES_DEFAULT_DB,
    MC_POSTGRES_DEFAULT_USER,
    MC_POSTGRES_PORT,
    MC_REDIS_PORT,
    MC_ROLE_BACKEND,
)
from ..coresys import CoreSysAttributes
from ..exceptions import DockerJobError
from ..jobs.const import JobConcurrency
from ..jobs.decorator import Job
from .const import ENV_TIME, DockerMount, MountType
from .interface import DockerInterface
from .mc_stack_base import (
    MC_POSTGRES_DNS_ALIASES,
    MC_REDIS_DNS_ALIASES,
    MC_STACK_RESTART_POLICY,
    mc_stack_container_ip,
    mc_stack_labels,
    mc_stack_networking_config,
)

_LOGGER: logging.Logger = logging.getLogger(__name__)

_BD_DATA_TARGET: Final[str] = "/mc/data"

_BD_ALIAS_PRIMARY: Final[str] = "mc_bd"
_BD_ALIAS_DNS: Final[str] = "mc-bd"


class DockerMcBackend(DockerInterface, CoreSysAttributes):
    """Docker Supervisor wrapper for the MC stack backend container."""

    @property
    def image(self) -> str | None:
        """Return image repository (no tag) from version data."""
        return self.sys_updater.image_mc_bd

    @property
    def name(self) -> str:
        """Return name of Docker container."""
        return MC_BACKEND_DOCKER_NAME

    @property
    def version(self) -> AwesomeVersion | None:  # type: ignore[override]
        """Return configured mc_bd version from updater."""
        return self.sys_updater.version_mc_bd

    @property
    def hostname(self) -> str:
        """Return container hostname (DNS-safe)."""
        return _BD_ALIAS_DNS

    @property
    def mounts(self) -> list[DockerMount]:
        """Return mounts for container."""
        return [
            DockerMount(
                type=MountType.BIND,
                source=self.sys_config.path_extern_mc_backend.as_posix(),
                target=_BD_DATA_TARGET,
                read_only=False,
            ),
        ]

    @property
    def environment(self) -> dict[str, str]:
        """Return mc_bd container environment (hostname defaults).

        Prefer :meth:`resolve_environment` at container start time so
        dependency IPs can be injected when Docker DNS is unreliable.
        """
        return self._base_environment()

    def _base_environment(self) -> dict[str, str]:
        """Build mc_bd env with logical hostnames for Postgres / Redis."""
        secrets = self.sys_mc_stack.secrets
        return {
            ENV_TIME: self.sys_timezone,
            "ENVIRONMENT": "prod",
            "DATABASE_TYPE": "postgresql",
            "DATABASE_HOST": "mc_postgres",
            "DATABASE_PORT": str(MC_POSTGRES_PORT),
            "DATABASE_USER": MC_POSTGRES_DEFAULT_USER,
            "DATABASE_PASSWORD": secrets.postgres_password,
            "DATABASE_SCHEMA": MC_POSTGRES_DEFAULT_DB,
            "REDIS_HOST": "mc_redis",
            "REDIS_PORT": str(MC_REDIS_PORT),
            "REDIS_PASSWORD": secrets.redis_password,
            "REDIS_DATABASE": "0",
            # mc_bd Granian listens on this port (Dockerfile EXPOSE 8001).
            "APP_PORT": str(MC_BACKEND_PORT),
        }

    async def resolve_environment(self) -> dict[str, str]:
        """Return mc_bd env, preferring dependency container IPs when known."""
        env = self._base_environment()
        for inst, host_key, alias in (
            (self.sys_mc_stack.postgres, "DATABASE_HOST", "mc_postgres"),
            (self.sys_mc_stack.redis, "REDIS_HOST", "mc_redis"),
        ):
            metadata = await self.sys_mc_stack.inspect_container(inst)
            if ip := mc_stack_container_ip(metadata):
                env[host_key] = str(ip)
                _LOGGER.debug("mc_bd %s resolved to %s (alias %s)", host_key, ip, alias)
        return env

    @property
    def labels(self) -> dict[str, str]:
        """Return container labels for monitoring / filtering."""
        return mc_stack_labels(MC_ROLE_BACKEND)

    @property
    def networking_config(self) -> dict[str, dict[str, dict]]:
        """Network endpoint config attaching to ``mcos`` with stack alias."""
        return mc_stack_networking_config(_BD_ALIAS_PRIMARY, _BD_ALIAS_DNS)

    @Job(
        name="docker_mc_backend_run",
        on_condition=DockerJobError,
        concurrency=JobConcurrency.GROUP_REJECT,
    )
    async def run(self) -> None:
        """Run mc_bd Docker image."""
        version = self.version
        if not version:
            raise DockerJobError(
                f"Cannot determine version for {self.name}", _LOGGER.error
            )

        extra_hosts = await self.sys_mc_stack.dependency_extra_hosts(
            (self.sys_mc_stack.postgres, MC_POSTGRES_DNS_ALIASES),
            (self.sys_mc_stack.redis, MC_REDIS_DNS_ALIASES),
        )
        environment = await self.resolve_environment()

        await self._run(
            tag=str(version),
            name=self.name,
            hostname=self.hostname,
            detach=True,
            security_opt=self.security_opt,
            environment=environment,
            mounts=self.mounts,
            networking_config=self.networking_config,
            labels=self.labels,
            restart_policy=MC_STACK_RESTART_POLICY,
            oom_score_adj=-200,
            extra_hosts=extra_hosts or None,
        )
        _LOGGER.info("Starting mc_bd %s with version %s", self.image, version)

    async def is_initialize(self) -> bool:
        """Return True if Docker container exists with the configured image."""
        if not self.image or not self.version:
            return False
        return await self.sys_docker.container_is_initialized(
            self.name, self.image, self.version
        )
