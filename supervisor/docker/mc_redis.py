"""Docker wrapper for the MC stack Redis container.

Redis sits between PostgreSQL and ``mc_bd`` in the start order and is
reachable inside the ``mcio`` Docker network under the alias ``mc_redis``
(matches ``mc_bd``'s ``REDIS_HOST='mc_redis'`` default). AOF persistence
is enabled by default so cached values survive container restarts; the
``mc_stack/redis`` data dir is bind-mounted to the standard ``/data``
target used by the official image.
"""

from __future__ import annotations

import logging
from typing import Final

from awesomeversion import AwesomeVersion

from ..const import MC_REDIS_DOCKER_NAME, MC_ROLE_REDIS
from ..coresys import CoreSysAttributes
from ..exceptions import DockerJobError
from ..jobs.const import JobConcurrency
from ..jobs.decorator import Job
from .const import ENV_TIME, DockerMount, MountType
from .interface import DockerInterface
from .mc_stack_base import (
    MC_STACK_RESTART_POLICY,
    mc_stack_labels,
    mc_stack_networking_config,
)

_LOGGER: logging.Logger = logging.getLogger(__name__)

# Container-internal data dir (Docker official redis image default).
_REDIS_DATA_TARGET: Final[str] = "/data"

_REDIS_ALIAS_PRIMARY: Final[str] = "mc_redis"
_REDIS_ALIAS_DNS: Final[str] = "mc-redis"


class DockerMcRedis(DockerInterface, CoreSysAttributes):
    """Docker Supervisor wrapper for the MC stack Redis container."""

    @property
    def image(self) -> str | None:
        """Return image repository (no tag) from version data."""
        return self.sys_updater.image_redis

    @property
    def name(self) -> str:
        """Return name of Docker container."""
        return MC_REDIS_DOCKER_NAME

    @property
    def version(self) -> AwesomeVersion | None:  # type: ignore[override]
        """Return configured Redis image tag from updater."""
        return self.sys_updater.version_redis

    @property
    def hostname(self) -> str:
        """Return container hostname (DNS-safe)."""
        return _REDIS_ALIAS_DNS

    @property
    def mounts(self) -> list[DockerMount]:
        """Return mounts for container."""
        return [
            DockerMount(
                type=MountType.BIND,
                source=self.sys_config.path_extern_mc_redis.as_posix(),
                target=_REDIS_DATA_TARGET,
                read_only=False,
            ),
        ]

    @property
    def environment(self) -> dict[str, str]:
        """Return Redis container environment."""
        return {ENV_TIME: self.sys_timezone}

    @property
    def command(self) -> list[str]:
        """Return entrypoint command for redis-server.

        Enable AOF persistence so values survive container restarts. If a
        Redis password is configured we apply it via ``--requirepass``.
        """
        cmd = ["redis-server", "--appendonly", "yes"]
        password = self.sys_mc_stack.secrets.redis_password
        if password:
            cmd.extend(["--requirepass", password])
        return cmd

    @property
    def labels(self) -> dict[str, str]:
        """Return container labels for monitoring / filtering."""
        return mc_stack_labels(MC_ROLE_REDIS)

    @property
    def networking_config(self) -> dict[str, dict[str, dict]]:
        """Network endpoint config attaching to ``mcio`` with stack alias."""
        return mc_stack_networking_config(_REDIS_ALIAS_PRIMARY, _REDIS_ALIAS_DNS)

    @Job(
        name="docker_mc_redis_run",
        on_condition=DockerJobError,
        concurrency=JobConcurrency.GROUP_REJECT,
    )
    async def run(self) -> None:
        """Run Redis Docker image."""
        version = self.version
        if not version:
            raise DockerJobError(
                f"Cannot determine version for {self.name}", _LOGGER.error
            )

        await self._run(
            tag=str(version),
            name=self.name,
            hostname=self.hostname,
            detach=True,
            security_opt=self.security_opt,
            environment=self.environment,
            mounts=self.mounts,
            networking_config=self.networking_config,
            command=self.command,
            labels=self.labels,
            restart_policy=MC_STACK_RESTART_POLICY,
            oom_score_adj=-200,
        )
        _LOGGER.info("Starting MC stack Redis %s with tag %s", self.image, version)

    async def is_initialize(self) -> bool:
        """Return True if Docker container exists with the configured image."""
        if not self.image or not self.version:
            return False
        return await self.sys_docker.container_is_initialized(
            self.name, self.image, self.version
        )
