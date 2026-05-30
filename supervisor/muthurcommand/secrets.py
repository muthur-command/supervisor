"""Handle Muthur Command secrets to add-ons."""

from datetime import timedelta
import logging
from pathlib import Path

from ..coresys import CoreSys, CoreSysAttributes
from ..exceptions import YamlFileError
from ..jobs.const import JobConcurrency, JobThrottle
from ..jobs.decorator import Job
from ..utils.yaml import read_yaml_file

_LOGGER: logging.Logger = logging.getLogger(__name__)


class MuthurCommandSecrets(CoreSysAttributes):
    """Manage Muthur Command secrets."""

    def __init__(self, coresys: CoreSys):
        """Initialize secret manager."""
        self.coresys: CoreSys = coresys
        self.secrets: dict[str, bool | float | int | str] = {}

    @property
    def path_secrets(self) -> Path:
        """Return path to secret file."""
        return Path(self.sys_config.path_muthurcommand, "secrets.yaml")

    def get(self, secret: str) -> bool | float | int | str | None:
        """Get secret from store."""
        _LOGGER.info("Request secret %s", secret)
        return self.secrets.get(secret)

    async def load(self) -> None:
        """Load secrets on start."""
        await self._read_secrets()

        _LOGGER.info("Loaded %s Muthur Command secrets", len(self.secrets))

    async def reload(self) -> None:
        """Reload secrets."""
        await self._read_secrets()

    @Job(
        name="muthurcommand_secrets_read",
        throttle_period=timedelta(seconds=60),
        internal=True,
        concurrency=JobConcurrency.QUEUE,
        throttle=JobThrottle.THROTTLE,
    )
    async def _read_secrets(self):
        """Read secrets.yaml into memory."""

        def read_secrets_yaml() -> dict | None:
            if not self.path_secrets.exists():
                _LOGGER.debug("Muthur Command secrets.yaml does not exist")
                return None

            # Read secrets
            try:
                return read_yaml_file(self.path_secrets)
            except YamlFileError as err:
                _LOGGER.warning("Can't read Muthur Command secrets: %s", err)
                return None

        secrets = await self.sys_run_in_executor(read_secrets_yaml)
        if secrets is None or not isinstance(secrets, dict):
            return

        # Process secrets
        self.secrets = {
            k: v for k, v in secrets.items() if isinstance(v, (bool, float, int, str))
        }
        _LOGGER.debug("Reloading Muthur Command secrets: %s", len(self.secrets))
