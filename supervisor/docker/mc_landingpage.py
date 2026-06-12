"""Docker wrapper for the MCOS bootstrap landingpage container.

The landingpage image is a lightweight Go static server that binds host port
8123 (``network_mode=host``) and shows first-boot progress while the MC stack
(``mc_bd`` / ``mc_fd``) is still coming up. Supervisor tears it down once
``mc_fd`` is healthy and re-publishes :data:`MC_FRONTEND_HOST_PORT` on
``mc_fd`` instead.
"""

from __future__ import annotations

import logging
from typing import Final

from awesomeversion import AwesomeVersion

from ..const import (
    MC_LANDINGPAGE_DOCKER_NAME,
    MC_ROLE_LANDINGPAGE,
)
from ..coresys import CoreSysAttributes
from ..exceptions import DockerJobError
from ..jobs.const import JobConcurrency
from ..jobs.decorator import Job
from ..muthurcommand.const import LANDINGPAGE
from .const import ENV_TIME, ENV_TOKEN, MOUNT_DBUS, MOUNT_DEV, MOUNT_UDEV
from .interface import DockerInterface
from .mc_stack_base import MC_STACK_RESTART_POLICY, mc_stack_labels

_LOGGER: logging.Logger = logging.getLogger(__name__)

_LANDINGPAGE_HOSTNAME: Final[str] = "landingpage"


class DockerMcLandingpage(DockerInterface, CoreSysAttributes):
    """Docker Supervisor wrapper for the MCOS bootstrap landingpage."""

    @property
    def image(self) -> str | None:
        """Return image repository (no tag) from version data."""
        return self.sys_updater.image_landingpage

    @property
    def name(self) -> str:
        """Return name of Docker container."""
        return MC_LANDINGPAGE_DOCKER_NAME

    @property
    def version(self) -> AwesomeVersion | None:  # type: ignore[override]
        """Return the fixed ``landingpage`` image tag."""
        if not self.image:
            return None
        return LANDINGPAGE

    @property
    def labels(self) -> dict[str, str]:
        """Return container labels for monitoring / filtering."""
        return mc_stack_labels(MC_ROLE_LANDINGPAGE)

    @Job(
        name="docker_mc_landingpage_run",
        on_condition=DockerJobError,
        concurrency=JobConcurrency.GROUP_REJECT,
    )
    async def run(self) -> None:
        """Run the landingpage container on the host network (port 8123)."""
        version = self.version
        if not version or not self.image:
            raise DockerJobError(
                f"Cannot determine landingpage image for {self.name}", _LOGGER.error
            )

        environment: dict[str, str] = {
            "SUPERVISOR": str(self.sys_docker.network.supervisor),
            ENV_TIME: self.sys_timezone,
        }
        if token := self.sys_muthurcommand.supervisor_token:
            environment[ENV_TOKEN] = token

        await self._run(
            tag=str(version),
            name=self.name,
            hostname=_LANDINGPAGE_HOSTNAME,
            detach=True,
            privileged=False,
            init=False,
            security_opt=self.security_opt,
            network_mode="host",
            mounts=[MOUNT_DEV, MOUNT_DBUS, MOUNT_UDEV],
            extra_hosts={
                "supervisor": self.sys_docker.network.supervisor,
                "observer": self.sys_docker.network.observer,
            },
            environment=environment,
            labels=self.labels,
            restart_policy=MC_STACK_RESTART_POLICY,
            tmpfs={"/tmp": ""},  # noqa: S108
            oom_score_adj=-300,
        )
        _LOGGER.info(
            "Starting MC landingpage %s with version %s", self.image, version
        )
