"""Docker wrapper for the MC stack PostgreSQL container.

The PostgreSQL container is the foundation of the MC application stack and
must come up before ``mc_redis`` / ``mc_bd``. Image tag, persistent volume
and credentials are decided by the Supervisor; the container is reachable
inside the ``mcio`` Docker network under the alias ``mc_postgres`` to match
``mc_bd``'s ``DATABASE_HOST='mc_postgres'`` default.

Resource notes:
    * PostgreSQL benefits from a ``/dev/shm`` larger than Docker's 64 MiB
      default. We bump it to 256 MiB which matches the Bitnami / official
      "small workload" recommendations.
    * The persistent volume lives at ``mc_stack/postgresql`` under
      Supervisor's data dir; the official image places its actual data
      one level deeper (``$PGDATA``) which we point at ``…/data/pgdata``
      so the bind-mount root stays free of leftover ``lost+found`` etc.
"""

from __future__ import annotations

import logging
from typing import Final

from awesomeversion import AwesomeVersion

from ..const import (
    MC_POSTGRES_DEFAULT_DB,
    MC_POSTGRES_DEFAULT_USER,
    MC_POSTGRES_DOCKER_NAME,
    MC_ROLE_POSTGRES,
)
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

# Container-internal data dir (Docker official postgres image default).
_PGDATA_TARGET: Final[str] = "/var/lib/postgresql/data"
# Network aliases mc_bd talks to (DATABASE_HOST='mc_postgres') and a DNS
# RFC-friendly hyphen variant for clients that normalise hostnames first.
_PG_ALIAS_PRIMARY: Final[str] = "mc_postgres"
_PG_ALIAS_DNS: Final[str] = "mc-postgres"
# 256 MiB shared memory; small but beats Docker's 64 MiB default.
_PG_SHM_SIZE: Final[int] = 256 * 1024 * 1024


class DockerMcPostgres(DockerInterface, CoreSysAttributes):
    """Docker Supervisor wrapper for the MC stack PostgreSQL container."""

    @property
    def image(self) -> str | None:
        """Return image repository (no tag) from version data."""
        return self.sys_updater.image_postgresql

    @property
    def name(self) -> str:
        """Return name of Docker container."""
        return MC_POSTGRES_DOCKER_NAME

    @property
    def version(self) -> AwesomeVersion | None:  # type: ignore[override]
        """Return configured PostgreSQL image tag from updater."""
        return self.sys_updater.version_postgresql

    @property
    def hostname(self) -> str:
        """Return container hostname (DNS-safe)."""
        return _PG_ALIAS_DNS

    @property
    def mounts(self) -> list[DockerMount]:
        """Return mounts for container."""
        return [
            DockerMount(
                type=MountType.BIND,
                source=self.sys_config.path_extern_mc_postgres.as_posix(),
                target=_PGDATA_TARGET,
                read_only=False,
            ),
        ]

    @property
    def environment(self) -> dict[str, str]:
        """Return PostgreSQL container environment."""
        return {
            ENV_TIME: self.sys_timezone,
            "POSTGRES_USER": MC_POSTGRES_DEFAULT_USER,
            "POSTGRES_PASSWORD": self.sys_mc_stack.secrets.postgres_password,
            "POSTGRES_DB": MC_POSTGRES_DEFAULT_DB,
            # Avoid using the bind-mount root as PGDATA so the official
            # image can manage initdb metadata in a clean subdirectory.
            "PGDATA": f"{_PGDATA_TARGET}/pgdata",
        }

    @property
    def labels(self) -> dict[str, str]:
        """Return container labels for monitoring / filtering."""
        return mc_stack_labels(MC_ROLE_POSTGRES)

    @property
    def networking_config(self) -> dict[str, dict[str, dict]]:
        """Network endpoint config attaching to ``mcio`` with stack alias."""
        return mc_stack_networking_config(_PG_ALIAS_PRIMARY, _PG_ALIAS_DNS)

    @Job(
        name="docker_mc_postgres_run",
        on_condition=DockerJobError,
        concurrency=JobConcurrency.GROUP_REJECT,
    )
    async def run(self) -> None:
        """Run PostgreSQL Docker image."""
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
            labels=self.labels,
            restart_policy=MC_STACK_RESTART_POLICY,
            shm_size=_PG_SHM_SIZE,
            oom_score_adj=-200,
        )
        _LOGGER.info(
            "Starting MC stack PostgreSQL %s with tag %s", self.image, version
        )

    async def is_initialize(self) -> bool:
        """Return True if Docker container exists with the configured image."""
        if not self.image or not self.version:
            return False
        return await self.sys_docker.container_is_initialized(
            self.name, self.image, self.version
        )
