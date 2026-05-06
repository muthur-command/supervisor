"""Persistent secrets for the MC application stack.

The MC stack (PostgreSQL → Redis → mc_bd → mc_fd) needs a stable PostgreSQL
super-user password and Redis password between Supervisor restarts. Storing
them in plain ``.env`` files would couple the Supervisor to a particular
``mc_bd`` repo layout; instead we keep them inside the same Supervisor data
volume that already holds ``muthurcommand.json`` etc. and inject them as
container environment variables at start time.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

import voluptuous as vol

from ..const import FILE_MC_STACK_SECRETS
from ..coresys import CoreSys, CoreSysAttributes
from ..utils.common import FileConfiguration

_LOGGER: logging.Logger = logging.getLogger(__name__)

ATTR_POSTGRES_PASSWORD = "postgres_password"
ATTR_REDIS_PASSWORD = "redis_password"

# We default Redis to no password. That matches the ``mc_bd`` reference
# ``.env.server`` (REDIS_PASSWORD='') and avoids breaking existing dev
# setups; an operator can still set one through the API later.
SCHEMA_MC_STACK_SECRETS = vol.Schema(
    {
        vol.Optional(ATTR_POSTGRES_PASSWORD): vol.All(str, vol.Length(min=1)),
        vol.Optional(ATTR_REDIS_PASSWORD, default=""): str,
    },
    extra=vol.REMOVE_EXTRA,
)


class MCStackSecrets(FileConfiguration, CoreSysAttributes):
    """Manage secrets injected into MC stack containers."""

    def __init__(self, coresys: CoreSys) -> None:
        """Initialize secrets store."""
        super().__init__(FILE_MC_STACK_SECRETS, SCHEMA_MC_STACK_SECRETS)
        # ``FileConfiguration.__init__`` aliases ``_data`` to a shared
        # module-level ``_DEFAULT`` dict that is reassigned on
        # ``read_data``. We may mutate ``_data`` lazily (when generating
        # the PostgreSQL password on first use) before ever calling
        # ``load_config``, so isolate ourselves with a fresh dict here.
        self._data = {}
        self.coresys: CoreSys = coresys

    @property
    def postgres_password(self) -> str:
        """Return PostgreSQL super-user password, generating one on first use."""
        if not self._data.get(ATTR_POSTGRES_PASSWORD):
            self._data[ATTR_POSTGRES_PASSWORD] = secrets.token_urlsafe(24)
            _LOGGER.info("Generated initial PostgreSQL super-user password")
        return self._data[ATTR_POSTGRES_PASSWORD]

    @property
    def redis_password(self) -> str:
        """Return Redis password (may be empty)."""
        return self._data.get(ATTR_REDIS_PASSWORD, "")

    async def ensure(self) -> None:
        """Ensure required secrets exist on disk and are persisted."""
        # Touch the PostgreSQL password property to lazily generate + persist.
        _ = self.postgres_password
        await self.save_data()

    def to_dict(self) -> dict[str, Any]:
        """Return current secrets snapshot (used by tests / diagnostics)."""
        return {
            ATTR_POSTGRES_PASSWORD: self.postgres_password,
            ATTR_REDIS_PASSWORD: self.redis_password,
        }
