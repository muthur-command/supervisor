"""Operator-tunable runtime configuration for the MC application stack.

While ``MCStackSecrets`` keeps PostgreSQL / Redis credentials, this module
keeps the **policy knobs** the operator can flip from the panel or the
REST API:

* ``boot`` — whether ``Core.start`` brings the MC stack up automatically.
* ``watchdog`` — whether the periodic watchdog tries to recover ``mc_bd``
  / restart the stack when the HTTP probe stops answering.

Both default to ``True`` to match the documented "MC stack is the user-
visible deliverable" behaviour. They are persisted to
``mc_stack.json`` so a Supervisor restart preserves the choice.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from ..const import FILE_MC_STACK
from ..coresys import CoreSys, CoreSysAttributes
from ..utils.common import FileConfiguration

_LOGGER: logging.Logger = logging.getLogger(__name__)

ATTR_BOOT = "boot"
ATTR_WATCHDOG = "watchdog"
ATTR_DUAL_FRONTEND = "dual_frontend"
ATTR_FRONTEND_ROUTE = "frontend_route"

_FRONTEND_ROUTE_LANDINGPAGE = "landingpage"

SCHEMA_MC_STACK_CONFIG = vol.Schema(
    {
        vol.Optional(ATTR_BOOT, default=True): bool,
        vol.Optional(ATTR_WATCHDOG, default=True): bool,
        vol.Optional(ATTR_DUAL_FRONTEND, default=True): bool,
        vol.Optional(
            ATTR_FRONTEND_ROUTE, default=_FRONTEND_ROUTE_LANDINGPAGE
        ): vol.Any("landingpage", "mc_fd"),
    },
    extra=vol.REMOVE_EXTRA,
)


class MCStackConfig(FileConfiguration, CoreSysAttributes):
    """Persistent runtime options for ``MCStack``."""

    def __init__(self, coresys: CoreSys) -> None:
        """Initialize the runtime config store."""
        super().__init__(FILE_MC_STACK, SCHEMA_MC_STACK_CONFIG)
        # ``FileConfiguration.__init__`` aliases ``_data`` to a shared
        # ``_DEFAULT`` dict; isolate it so writes don't bleed across
        # other ``FileConfiguration`` instances (see MCStackSecrets).
        self._data = SCHEMA_MC_STACK_CONFIG({})
        self.coresys: CoreSys = coresys

    @property
    def boot(self) -> bool:
        """Return True if the MC stack should auto-start with the Supervisor."""
        return self._data[ATTR_BOOT]

    @boot.setter
    def boot(self, value: bool) -> None:
        """Persist whether ``Core.start`` should bring up the MC stack."""
        self._data[ATTR_BOOT] = bool(value)

    @property
    def watchdog(self) -> bool:
        """Return True if the periodic watchdog may recover the stack."""
        return self._data[ATTR_WATCHDOG]

    @watchdog.setter
    def watchdog(self, value: bool) -> None:
        """Persist whether the periodic watchdog may act on the stack."""
        self._data[ATTR_WATCHDOG] = bool(value)

    @property
    def dual_frontend(self) -> bool:
        """Return True when bootstrap landingpage routing is allowed."""
        return self._data[ATTR_DUAL_FRONTEND]

    @dual_frontend.setter
    def dual_frontend(self, value: bool) -> None:
        """Persist whether the dual-frontend bootstrap flow is enabled."""
        self._data[ATTR_DUAL_FRONTEND] = bool(value)

    @property
    def frontend_route(self) -> str:
        """Return which frontend currently owns host port 8123."""
        return self._data[ATTR_FRONTEND_ROUTE]

    @frontend_route.setter
    def frontend_route(self, value: str) -> None:
        """Persist the active frontend route."""
        self._data[ATTR_FRONTEND_ROUTE] = value

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly snapshot of the current runtime config."""
        return {
            ATTR_BOOT: self.boot,
            ATTR_WATCHDOG: self.watchdog,
            ATTR_DUAL_FRONTEND: self.dual_frontend,
            ATTR_FRONTEND_ROUTE: self.frontend_route,
        }
