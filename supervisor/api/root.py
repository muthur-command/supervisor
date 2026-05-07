"""Init file for Supervisor Root RESTful API."""

import asyncio
import logging
from typing import Any

from aiohttp import web

from ..const import (
    ATTR_ARCH,
    ATTR_CHANNEL,
    ATTR_DOCKER,
    ATTR_ENABLED,
    ATTR_FEATURES,
    ATTR_HOSTNAME,
    ATTR_ICON,
    ATTR_LOGGING,
    ATTR_MACHINE,
    ATTR_MACHINE_ID,
    ATTR_MC_BD,
    ATTR_MC_FD,
    ATTR_MCOS,
    ATTR_MUTHURCOMMAND,
    ATTR_NAME,
    ATTR_OPERATING_SYSTEM,
    ATTR_POSTGRESQL,
    ATTR_REDIS,
    ATTR_STATE,
    ATTR_SUPERVISOR,
    ATTR_SUPPORTED,
    ATTR_SUPPORTED_ARCH,
    ATTR_TIMEZONE,
    ATTR_VERSION,
    ATTR_VERSION_LATEST,
)
from ..coresys import CoreSysAttributes
from .const import ATTR_AVAILABLE_UPDATES, ATTR_PANEL_PATH, ATTR_UPDATE_TYPE
from .utils import api_process

ATTR_MC_STACK = "mc_stack"

_LOGGER: logging.Logger = logging.getLogger(__name__)


class APIRoot(CoreSysAttributes):
    """Handle RESTful API for root functions."""

    @api_process
    async def info(self, request: web.Request) -> dict[str, Any]:
        """Show system info."""
        return {
            ATTR_SUPERVISOR: self.sys_supervisor.version,
            ATTR_MUTHURCOMMAND: self.sys_muthurcommand.version,
            ATTR_MCOS: self.sys_os.version,
            ATTR_DOCKER: self.sys_docker.info.version,
            ATTR_HOSTNAME: self.sys_host.info.hostname,
            ATTR_OPERATING_SYSTEM: self.sys_host.info.operating_system,
            ATTR_FEATURES: self.sys_host.features,
            ATTR_MACHINE: self.sys_machine,
            ATTR_MACHINE_ID: self.sys_machine_id,
            ATTR_ARCH: self.sys_arch.default,
            ATTR_STATE: self.sys_core.state,
            ATTR_SUPPORTED_ARCH: self.sys_arch.supported,
            ATTR_SUPPORTED: self.sys_core.supported,
            ATTR_CHANNEL: self.sys_updater.channel,
            ATTR_LOGGING: self.sys_config.logging,
            ATTR_TIMEZONE: self.sys_timezone,
            # MC application stack version snapshot (Stage 6 of A1 plan).
            # ``enabled`` lets clients hide the legacy HA Core panels when
            # the MC stack is the user-facing entry point.
            ATTR_MC_STACK: {
                ATTR_ENABLED: self.sys_mc_stack.enabled,
                ATTR_MC_BD: {
                    ATTR_VERSION: self.sys_mc_stack.backend.version,
                    ATTR_VERSION_LATEST: self.sys_updater.version_mc_bd,
                },
                ATTR_MC_FD: {
                    ATTR_VERSION: self.sys_mc_stack.frontend.version,
                    ATTR_VERSION_LATEST: self.sys_updater.version_mc_fd,
                },
                ATTR_POSTGRESQL: {
                    ATTR_VERSION: self.sys_mc_stack.postgres.version,
                    ATTR_VERSION_LATEST: self.sys_updater.version_postgresql,
                },
                ATTR_REDIS: {
                    ATTR_VERSION: self.sys_mc_stack.redis.version,
                    ATTR_VERSION_LATEST: self.sys_updater.version_redis,
                },
            },
        }

    @api_process
    async def available_updates(self, request: web.Request) -> dict[str, Any]:
        """Return a list of items with available updates."""
        available_updates = []

        # Core
        if self.sys_muthurcommand.need_update:
            available_updates.append(
                {
                    ATTR_UPDATE_TYPE: "mc_bd",
                    ATTR_PANEL_PATH: "/update-available/mc_bd",
                    ATTR_VERSION_LATEST: self.sys_muthurcommand.latest_version,
                }
            )

        # MC stack components (Stage 6: every application-half component
        # gets its own update entry so the panel can offer them
        # independently). ``MCStack.update`` orchestrates them as a
        # single transaction when invoked.
        if self.sys_mc_stack.enabled:
            for kind, current, latest in (
                (
                    "mc_bd",
                    self.sys_mc_stack.backend.version,
                    self.sys_updater.version_mc_bd,
                ),
                (
                    "mc_fd",
                    self.sys_mc_stack.frontend.version,
                    self.sys_updater.version_mc_fd,
                ),
                (
                    "postgresql",
                    self.sys_mc_stack.postgres.version,
                    self.sys_updater.version_postgresql,
                ),
                (
                    "redis",
                    self.sys_mc_stack.redis.version,
                    self.sys_updater.version_redis,
                ),
            ):
                if latest is None or current is None or current >= latest:
                    continue
                available_updates.append(
                    {
                        ATTR_UPDATE_TYPE: kind,
                        ATTR_PANEL_PATH: f"/update-available/{kind}",
                        ATTR_VERSION_LATEST: latest,
                    }
                )

        # Supervisor
        if self.sys_supervisor.need_update:
            available_updates.append(
                {
                    ATTR_UPDATE_TYPE: "supervisor",
                    ATTR_PANEL_PATH: "/update-available/supervisor",
                    ATTR_VERSION_LATEST: self.sys_supervisor.latest_version,
                }
            )

        # OS
        if self.sys_os.need_update:
            available_updates.append(
                {
                    ATTR_UPDATE_TYPE: "os",
                    ATTR_PANEL_PATH: "/update-available/os",
                    ATTR_VERSION_LATEST: self.sys_os.latest_version,
                }
            )

        # Add-ons
        available_updates.extend(
            {
                ATTR_UPDATE_TYPE: "addon",
                ATTR_NAME: addon.name,
                ATTR_ICON: f"/addons/{addon.slug}/icon" if addon.with_icon else None,
                ATTR_PANEL_PATH: f"/update-available/{addon.slug}",
                ATTR_VERSION_LATEST: addon.latest_version,
            }
            for addon in self.sys_addons.installed
            if addon.need_update
        )

        return {ATTR_AVAILABLE_UPDATES: available_updates}

    @api_process
    async def refresh_updates(self, request: web.Request) -> None:
        """Refresh All system updates information."""
        await asyncio.shield(
            asyncio.gather(self.sys_updater.reload(), self.sys_store.reload())
        )

    @api_process
    async def reload_updates(self, request: web.Request) -> None:
        """Refresh updater update information."""
        await self.sys_updater.reload()
