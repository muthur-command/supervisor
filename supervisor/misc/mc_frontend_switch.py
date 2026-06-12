"""Runtime routing between bootstrap landingpage and mc_fd on port 8123."""

from __future__ import annotations

from enum import StrEnum
import logging
from typing import Final

from ..const import ATTR_DUAL_FRONTEND, ATTR_FRONTEND_ROUTE
from ..coresys import CoreSysAttributes
from .mc_stack_config import MCStackConfig

_LOGGER: logging.Logger = logging.getLogger(__name__)

_FRONTEND_ROUTE_LANDINGPAGE: Final[str] = "landingpage"
_FRONTEND_ROUTE_MC_FD: Final[str] = "mc_fd"


class FrontendRoute(StrEnum):
    """Which user-facing frontend currently owns host port 8123."""

    LANDINGPAGE = _FRONTEND_ROUTE_LANDINGPAGE
    MC_FD = _FRONTEND_ROUTE_MC_FD


class MCFrontendSwitch(CoreSysAttributes):
    """Persisted route selector for the MCOS dual-frontend bootstrap flow."""

    def __init__(self, coresys, config: MCStackConfig) -> None:
        """Bind to the shared ``mc_stack.json`` config store."""
        self.coresys = coresys
        self._config = config

    @property
    def dual_frontend_enabled(self) -> bool:
        """Return True when landingpage should front the stack during bootstrap."""
        return (
            self._config.dual_frontend
            and self.sys_muthurcommand.unused
            and self.sys_updater.image_landingpage is not None
            and self.sys_updater.version_landingpage is not None
        )

    @property
    def route(self) -> FrontendRoute:
        """Return the active user-facing frontend route."""
        raw = self._config.frontend_route
        try:
            return FrontendRoute(raw)
        except ValueError:
            return FrontendRoute.LANDINGPAGE

    @route.setter
    def route(self, value: FrontendRoute) -> None:
        """Persist a route change (call ``save_data`` separately)."""
        if self._config.frontend_route == value.value:
            return
        _LOGGER.info("MC frontend route: %s → %s", self._config.frontend_route, value)
        self._config.frontend_route = value.value

    @property
    def publish_mc_fd_host_port(self) -> bool:
        """Return True when ``mc_fd`` should bind :data:`MC_FRONTEND_HOST_PORT`."""
        if not self.sys_muthurcommand.unused:
            # Legacy Muthur Command Core (or its landingpage) owns :8123.
            return False
        if not self.dual_frontend_enabled:
            return True
        return self.route == FrontendRoute.MC_FD

    def to_dict(self) -> dict[str, str | bool]:
        """Return a JSON-friendly snapshot for ``/info``."""
        return {
            ATTR_DUAL_FRONTEND: self.dual_frontend_enabled,
            ATTR_FRONTEND_ROUTE: self.route.value,
        }
