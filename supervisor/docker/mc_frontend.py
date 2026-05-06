"""Docker wrapper for the MC stack frontend (mc_fd) container.

The ``mc_fd`` Nginx static-site container is the user-facing entry point of
the MC application stack. It proxies API calls to ``mc_bd`` over the
internal ``mcio`` network and exposes itself via a published port so the
operator (or HA OS Ingress) can reach the login page.
"""

from __future__ import annotations

import logging
from typing import Final

from awesomeversion import AwesomeVersion

from ..const import (
    MC_BACKEND_PORT,
    MC_FRONTEND_DOCKER_NAME,
    MC_ROLE_FRONTEND,
)
from ..coresys import CoreSysAttributes
from ..exceptions import DockerJobError
from ..jobs.const import JobConcurrency
from ..jobs.decorator import Job
from .const import ENV_TIME
from .interface import DockerInterface
from .mc_stack_base import (
    MC_STACK_RESTART_POLICY,
    mc_stack_labels,
    mc_stack_networking_config,
)

_LOGGER: logging.Logger = logging.getLogger(__name__)

_FD_ALIAS_PRIMARY: Final[str] = "mc_fd"
_FD_ALIAS_DNS: Final[str] = "mc-fd"


class DockerMcFrontend(DockerInterface, CoreSysAttributes):
    """Docker Supervisor wrapper for the MC stack frontend container."""

    @property
    def image(self) -> str | None:
        """Return image repository (no tag) from version data."""
        return self.sys_updater.image_mc_fd

    @property
    def name(self) -> str:
        """Return name of Docker container."""
        return MC_FRONTEND_DOCKER_NAME

    @property
    def version(self) -> AwesomeVersion | None:  # type: ignore[override]
        """Return configured mc_fd version from updater."""
        return self.sys_updater.version_mc_fd

    @property
    def hostname(self) -> str:
        """Return container hostname (DNS-safe)."""
        return _FD_ALIAS_DNS

    @property
    def environment(self) -> dict[str, str]:
        """Return mc_fd container environment.

        ``mc_fd`` is a static Nginx site, but the build accepts upstream
        overrides via env so the runtime template can rewrite the
        ``proxy_pass`` target if the operator changes ports.
        """
        return {
            ENV_TIME: self.sys_timezone,
            "MC_BACKEND_HOST": "mc_bd",
            "MC_BACKEND_PORT": str(MC_BACKEND_PORT),
            # Reflect VITE_SERVER_API_PREFIX from the build (mc_fd .env.example).
            "VITE_SERVER_API_PREFIX": "/api",
        }

    @property
    def labels(self) -> dict[str, str]:
        """Return container labels for monitoring / filtering."""
        return mc_stack_labels(MC_ROLE_FRONTEND)

    @property
    def networking_config(self) -> dict[str, dict[str, dict]]:
        """Network endpoint config attaching to ``mcio`` with stack alias."""
        return mc_stack_networking_config(_FD_ALIAS_PRIMARY, _FD_ALIAS_DNS)

    @Job(
        name="docker_mc_frontend_run",
        on_condition=DockerJobError,
        concurrency=JobConcurrency.GROUP_REJECT,
    )
    async def run(self) -> None:
        """Run mc_fd Docker image."""
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
            networking_config=self.networking_config,
            labels=self.labels,
            restart_policy=MC_STACK_RESTART_POLICY,
            oom_score_adj=-300,
        )
        _LOGGER.info("Starting mc_fd %s with version %s", self.image, version)

    async def is_initialize(self) -> bool:
        """Return True if Docker container exists with the configured image."""
        if not self.image or not self.version:
            return False
        return await self.sys_docker.container_is_initialized(
            self.name, self.image, self.version
        )
