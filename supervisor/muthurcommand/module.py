"""Muthur Command control object."""

import asyncio
from ipaddress import IPv4Address
import logging
from pathlib import Path, PurePath
import shutil
import tarfile
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

from awesomeversion import AwesomeVersion, AwesomeVersionException
from securetar import AddFileError, SecureTarFile, atomic_contents_add
import voluptuous as vol
from voluptuous.humanize import humanize_error

from ..const import (
    ATTR_ACCESS_TOKEN,
    ATTR_AUDIO_INPUT,
    ATTR_AUDIO_OUTPUT,
    ATTR_BACKUPS_EXCLUDE_DATABASE,
    ATTR_BOOT,
    ATTR_DUPLICATE_LOG_FILE,
    ATTR_IMAGE,
    ATTR_MESSAGE,
    ATTR_PORT,
    ATTR_REFRESH_TOKEN,
    ATTR_SSL,
    ATTR_TYPE,
    ATTR_UUID,
    ATTR_VERSION,
    ATTR_WATCHDOG,
    FILE_MUTHURCOMMAND,
    BusEvent,
    MuthurCommandUser,
)
from ..coresys import CoreSys, CoreSysAttributes
from ..exceptions import (
    BackupInvalidError,
    ConfigurationFileError,
    MuthurCommandBackupError,
    MuthurCommandError,
    MuthurCommandWSError,
)
from ..hardware.const import PolicyGroup
from ..hardware.data import Device
from ..jobs.decorator import Job
from ..utils import remove_folder, remove_folder_with_excludes
from ..utils.common import FileConfiguration
from ..utils.json import read_json_file, write_json_file
from .api import MuthurCommandAPI
from .const import ATTR_ERROR, ATTR_OVERRIDE_IMAGE, ATTR_SUCCESS, LANDINGPAGE, WSType
from .core import MuthurCommandCore
from .secrets import MuthurCommandSecrets
from .validate import SCHEMA_HASS_CONFIG
from .websocket import MuthurCommandWebSocket

_LOGGER: logging.Logger = logging.getLogger(__name__)


MUTHURCOMMAND_BACKUP_EXCLUDE = [
    "**/__pycache__/*",
    "**/.DS_Store",
    "*.db-shm",
    "*.corrupt.*",
    "*.log.*",
    "*.log",
    ".storage/*.corrupt.*",
    "OZW_Log.txt",
    "backups/*.tar",
    "tmp_backups/*.tar",
    "tts/*",
    ".cache/*",
]
MUTHURCOMMAND_BACKUP_EXCLUDE_DATABASE = [
    "muthurcommand_v?.db",
    "muthurcommand_v?.db-wal",
]


class MuthurCommand(FileConfiguration, CoreSysAttributes):
    """Muthur Command core object for handle it."""

    def __init__(self, coresys: CoreSys):
        """Initialize Muthur Command object."""
        super().__init__(FILE_MUTHURCOMMAND, SCHEMA_HASS_CONFIG)
        self.coresys: CoreSys = coresys
        self._api: MuthurCommandAPI = MuthurCommandAPI(coresys)
        self._websocket: MuthurCommandWebSocket = MuthurCommandWebSocket(coresys)
        self._core: MuthurCommandCore = MuthurCommandCore(coresys)
        self._secrets: MuthurCommandSecrets = MuthurCommandSecrets(coresys)

    @property
    def api(self) -> MuthurCommandAPI:
        """Return API handler for core."""
        return self._api

    @property
    def websocket(self) -> MuthurCommandWebSocket:
        """Return Websocket handler for core."""
        return self._websocket

    @property
    def core(self) -> MuthurCommandCore:
        """Return Core handler for docker."""
        return self._core

    @property
    def secrets(self) -> MuthurCommandSecrets:
        """Return Secrets Manager for core."""
        return self._secrets

    @property
    def machine(self) -> str | None:
        """Return the system machines."""
        return self.core.instance.machine

    @property
    def arch(self) -> str | None:
        """Return arch of running Muthur Command."""
        return self.core.instance.arch

    @property
    def error_state(self) -> bool:
        """Return True if system is in error."""
        return self.core.error_state

    @property
    def ip_address(self) -> IPv4Address:
        """Return IP of Muthur Command instance."""
        return self.core.instance.ip_address

    @property
    def api_port(self) -> int:
        """Return network port to Muthur Command instance."""
        return self._data[ATTR_PORT]

    @api_port.setter
    def api_port(self, value: int) -> None:
        """Set network port for Muthur Command instance."""
        self._data[ATTR_PORT] = value

    @property
    def api_ssl(self) -> bool:
        """Return if we need ssl to Muthur Command instance."""
        return self._data[ATTR_SSL]

    @api_ssl.setter
    def api_ssl(self, value: bool):
        """Set SSL for Muthur Command instance."""
        self._data[ATTR_SSL] = value

    @property
    def api_url(self) -> str:
        """Return API url to Muthur Command."""
        return (
            f"{'https' if self.api_ssl else 'http'}://{self.ip_address}:{self.api_port}"
        )

    @property
    def ws_url(self) -> str:
        """Return API url to Muthur Command."""
        return f"{'wss' if self.api_ssl else 'ws'}://{self.ip_address}:{self.api_port}/api/websocket"

    @property
    def watchdog(self) -> bool:
        """Return True if the watchdog should protect Muthur Command."""
        return self._data[ATTR_WATCHDOG]

    @watchdog.setter
    def watchdog(self, value: bool):
        """Return True if the watchdog should protect Muthur Command."""
        self._data[ATTR_WATCHDOG] = value

    @property
    def latest_version(self) -> AwesomeVersion | None:
        """Return last available version of Muthur Command."""
        return self.sys_updater.version_muthurcommand

    @property
    def default_image(self) -> str:
        """Return the default image for this system."""
        # Repository must not include a Docker tag; version is appended as ``:tag``
        # elsewhere (see ``DockerInterface.check_image``). Board goes in the path.
        return (
            f"ghcr.io/muthur-command/{self.sys_arch.supervisor}-muthurcommand-"
            f"{self.sys_machine}"
        )

    @property
    def image(self) -> str:
        """Return image name of the Muthur Command container."""
        if self._data.get(ATTR_IMAGE):
            return self._data[ATTR_IMAGE]
        return self.default_image

    def set_image(self, value: str | None) -> None:
        """Set image name of Muthur Command container."""
        self._data[ATTR_IMAGE] = value

    @property
    def override_image(self) -> bool:
        """Return if user has overridden the image to use for Muthur Command."""
        return self._data[ATTR_OVERRIDE_IMAGE]

    @override_image.setter
    def override_image(self, value: bool) -> None:
        """Enable/disable image override."""
        self._data[ATTR_OVERRIDE_IMAGE] = value

    @property
    def version(self) -> AwesomeVersion | None:
        """Return version of local version."""
        return self._data.get(ATTR_VERSION)

    @version.setter
    def version(self, value: AwesomeVersion) -> None:
        """Set installed version."""
        self._data[ATTR_VERSION] = value

    @property
    def boot(self) -> bool:
        """Return True if Muthur Command boot is enabled."""
        return self._data[ATTR_BOOT]

    @boot.setter
    def boot(self, value: bool):
        """Set Muthur Command boot options."""
        self._data[ATTR_BOOT] = value

    @property
    def uuid(self) -> UUID:
        """Return a UUID of this Muthur Command instance."""
        return self._data[ATTR_UUID]

    @property
    def supervisor_token(self) -> str | None:
        """Return an access token for the Supervisor API."""
        return self._data.get(ATTR_ACCESS_TOKEN)

    @supervisor_token.setter
    def supervisor_token(self, value: str) -> None:
        """Set the access token for the Supervisor API."""
        self._data[ATTR_ACCESS_TOKEN] = value

    @property
    def refresh_token(self) -> str | None:
        """Return the refresh token to authenticate with Muthur Command."""
        return self._data.get(ATTR_REFRESH_TOKEN)

    @refresh_token.setter
    def refresh_token(self, value: str | None):
        """Set Muthur Command refresh_token."""
        self._data[ATTR_REFRESH_TOKEN] = value

    @property
    def path_pulse(self) -> Path:
        """Return path to asound config."""
        return Path(self.sys_config.path_tmp, "muthurcommand_pulse")

    @property
    def path_extern_pulse(self) -> PurePath:
        """Return path to asound config for Docker."""
        return PurePath(self.sys_config.path_extern_tmp, "muthurcommand_pulse")

    @property
    def audio_output(self) -> str | None:
        """Return a pulse profile for output or None."""
        return self._data[ATTR_AUDIO_OUTPUT]

    @audio_output.setter
    def audio_output(self, value: str | None):
        """Set audio output profile settings."""
        self._data[ATTR_AUDIO_OUTPUT] = value

    @property
    def audio_input(self) -> str | None:
        """Return pulse profile for input or None."""
        return self._data[ATTR_AUDIO_INPUT]

    @audio_input.setter
    def audio_input(self, value: str | None):
        """Set audio input settings."""
        self._data[ATTR_AUDIO_INPUT] = value

    @property
    def need_update(self) -> bool:
        """Return true if a Muthur Command update is available."""
        try:
            return self.version is not None and self.version < self.latest_version
        except (AwesomeVersionException, TypeError):
            return False

    @property
    def unused(self) -> bool:
        """Return True when this MCOS variant ships no Muthur Command Core.

        The version JSON marks per-machine Core slots as ``"unused"``
        for MCOS images that rely on the MC stack instead of the legacy
        single-container Core. The Updater reduces that to
        ``version_muthurcommand = None``; combined with no locally
        installed version, we treat the Core path as inactive.
        """
        return self.version is None and self.latest_version is None

    @property
    def backups_exclude_database(self) -> bool:
        """Exclude database from core backups by default."""
        return self._data[ATTR_BACKUPS_EXCLUDE_DATABASE]

    @backups_exclude_database.setter
    def backups_exclude_database(self, value: bool) -> None:
        """Set whether backups should exclude database by default."""
        self._data[ATTR_BACKUPS_EXCLUDE_DATABASE] = value

    @property
    def duplicate_log_file(self) -> bool:
        """Return True if Muthur Command should duplicate logs to file."""
        return self._data[ATTR_DUPLICATE_LOG_FILE]

    @duplicate_log_file.setter
    def duplicate_log_file(self, value: bool) -> None:
        """Set whether Muthur Command should duplicate logs to file."""
        self._data[ATTR_DUPLICATE_LOG_FILE] = value

    async def load(self) -> None:
        """Prepare Muthur Command object."""
        await asyncio.wait(
            [
                self.sys_create_task(self.websocket.load()),
                self.sys_create_task(self.secrets.load()),
                self.sys_create_task(self.core.load()),
            ]
        )

        # Register for events
        self.sys_bus.register_event(BusEvent.HARDWARE_NEW_DEVICE, self._hardware_events)
        self.sys_bus.register_event(
            BusEvent.HARDWARE_REMOVE_DEVICE, self._hardware_events
        )

    async def write_pulse(self):
        """Write asound config to file and return True on success."""
        pulse_config = self.sys_plugins.audio.pulse_client(
            input_profile=self.audio_input, output_profile=self.audio_output
        )

        def write_pulse_config():
            # Cleanup wrong maps
            if self.path_pulse.is_dir():
                shutil.rmtree(self.path_pulse, ignore_errors=True)
            self.path_pulse.write_text(pulse_config, encoding="utf-8")

        try:
            await self.sys_run_in_executor(write_pulse_config)
        except OSError as err:
            self.sys_resolution.check_oserror(err)
            _LOGGER.error("Muthur Command can't write pulse/client.config: %s", err)
        else:
            _LOGGER.info("Update pulse/client.config: %s", self.path_pulse)

    async def _hardware_events(self, device: Device) -> None:
        """Process hardware requests."""
        if self.unused:
            # No Muthur Command Core to forward USB-rescan events to.
            return
        if (
            not self.sys_hardware.policy.is_match_cgroup(PolicyGroup.UART, device)
            or not self.version
            or self.version == LANDINGPAGE
            or self.version < "2021.9.0"
        ):
            return

        try:
            configuration: (
                dict[str, Any] | None
            ) = await self.sys_muthurcommand.websocket.async_send_command(
                {ATTR_TYPE: "get_config"}
            )
        except MuthurCommandWSError as err:
            _LOGGER.warning(
                "Can't get Muthur Command Core configuration: %s. Not sending hardware events to Muthur Command Core.",
                err,
            )
            return

        if not configuration or "usb" not in configuration.get("components", []):
            return

        self.sys_muthurcommand.websocket.send_command({ATTR_TYPE: "usb/scan"})

    @Job(name="muthurcommand_module_begin_backup")
    async def begin_backup(self) -> None:
        """Inform Muthur Command a backup is beginning."""
        try:
            resp: dict[str, Any] | None = await self.websocket.async_send_command(
                {ATTR_TYPE: WSType.BACKUP_START}
            )
        except MuthurCommandWSError as err:
            raise MuthurCommandBackupError(
                f"Preparing backup of Muthur Command Core failed. Failed to inform Muthur Command Core: {str(err)}.",
                _LOGGER.error,
            ) from err

        if resp and not resp.get(ATTR_SUCCESS):
            raise MuthurCommandBackupError(
                f"Preparing backup of Muthur Command Core failed due to: {resp.get(ATTR_ERROR, {}).get(ATTR_MESSAGE, '')}. Check Muthur Command Core logs.",
                _LOGGER.error,
            )

    @Job(name="muthurcommand_module_end_backup")
    async def end_backup(self) -> None:
        """Inform Muthur Command the backup is ending."""
        try:
            resp: dict[str, Any] | None = await self.websocket.async_send_command(
                {ATTR_TYPE: WSType.BACKUP_END}
            )
        except MuthurCommandWSError as err:
            _LOGGER.warning(
                "Error resuming normal operations after backup of Muthur Command Core. Failed to inform Muthur Command Core: %s.",
                str(err),
            )
        else:
            if resp and not resp.get(ATTR_SUCCESS):
                _LOGGER.warning(
                    "Error resuming normal operations after backup of Muthur Command Core due to: %s. Check Muthur Command Core logs.",
                    resp.get(ATTR_ERROR, {}).get(ATTR_MESSAGE, ""),
                )

    @Job(name="muthurcommand_module_backup")
    async def backup(
        self, tar_file: SecureTarFile, exclude_database: bool = False
    ) -> None:
        """Backup Muthur Command Core config/directory."""
        excludes = MUTHURCOMMAND_BACKUP_EXCLUDE.copy()
        if exclude_database:
            excludes += MUTHURCOMMAND_BACKUP_EXCLUDE_DATABASE

        def _is_excluded_by_filter(path: PurePath) -> bool:
            """Filter function to filter out excluded files from the backup."""
            for exclude in excludes:
                if not path.full_match(f"data/{exclude}"):
                    continue
                _LOGGER.debug("Ignoring %s because of %s", path, exclude)
                return True

            return False

        # Backup data config folder
        def _write_tarfile(metadata: dict[str, Any]) -> None:
            """Write tarfile."""
            with TemporaryDirectory(dir=self.sys_config.path_tmp) as temp:
                temp_path = Path(temp)

                # Store local configs/state
                try:
                    write_json_file(temp_path.joinpath("muthurcommand.json"), metadata)
                except ConfigurationFileError as err:
                    raise MuthurCommandError(
                        f"Can't save meta for Muthur Command Core: {err!s}",
                        _LOGGER.error,
                    ) from err

                try:
                    with tar_file as backup:
                        # Backup metadata
                        backup.add(temp, arcname=".")

                        # Backup data
                        atomic_contents_add(
                            backup,
                            self.sys_config.path_muthurcommand,
                            file_filter=_is_excluded_by_filter,
                            arcname="data",
                        )
                except (tarfile.TarError, OSError, AddFileError) as err:
                    raise MuthurCommandBackupError(
                        f"Can't backup Muthur Command Core config folder: {str(err)}",
                        _LOGGER.error,
                    ) from err

        await self.begin_backup()
        try:
            _LOGGER.info("Backing up Muthur Command Core config folder")
            await self.sys_run_in_executor(_write_tarfile, self._data)
            _LOGGER.info("Backup Muthur Command Core config folder done")
        finally:
            await self.end_backup()

    @Job(name="muthurcommand_module_restore")
    async def restore(
        self, tar_file: SecureTarFile, exclude_database: bool | None = False
    ) -> None:
        """Restore Muthur Command Core config/ directory."""

        def _restore_muthurcommand() -> Any:
            """Restores data and reads metadata from backup.

            Returns: Muthur Command metdata
            """
            with TemporaryDirectory(dir=self.sys_config.path_tmp) as temp:
                temp_path = Path(temp)
                temp_data = temp_path.joinpath("data")
                temp_meta = temp_path.joinpath("muthurcommand.json")

                # extract backup
                try:
                    with tar_file as backup:
                        # The tar filter rejects path traversal and absolute names,
                        # aborting restore of potentially crafted backups.
                        backup.extractall(
                            path=temp_path,
                            filter="tar",
                        )
                except tarfile.FilterError as err:
                    raise BackupInvalidError(
                        f"Invalid tarfile {tar_file}: {err}", _LOGGER.error
                    ) from err
                except tarfile.TarError as err:
                    raise MuthurCommandError(
                        f"Can't read tarfile {tar_file}: {err}", _LOGGER.error
                    ) from err

                # Check old backup format v1
                if not temp_data.exists():
                    temp_data = temp_path

                _LOGGER.info("Restore Muthur Command Core config folder")
                if exclude_database is True:
                    remove_folder_with_excludes(
                        self.sys_config.path_muthurcommand,
                        excludes=MUTHURCOMMAND_BACKUP_EXCLUDE_DATABASE,
                        tmp_dir=self.sys_config.path_tmp,
                    )
                else:
                    remove_folder(self.sys_config.path_muthurcommand, content_only=True)

                try:
                    shutil.copytree(
                        temp_data,
                        self.sys_config.path_muthurcommand,
                        symlinks=True,
                        dirs_exist_ok=True,
                    )
                except shutil.Error as err:
                    raise MuthurCommandError(
                        f"Can't restore origin data: {err}", _LOGGER.error
                    ) from err

                _LOGGER.info("Restore Muthur Command Core config folder done")

                if not temp_meta.exists():
                    return None
                _LOGGER.info("Restore Muthur Command Core metadata")

                # Read backup data
                try:
                    data = read_json_file(temp_meta)
                except ConfigurationFileError as err:
                    raise MuthurCommandError() from err

                return data

        data = await self.sys_run_in_executor(_restore_muthurcommand)
        if data is None:
            return

        # Validate metadata
        try:
            data = SCHEMA_HASS_CONFIG(data)
        except vol.Invalid as err:
            raise MuthurCommandError(
                f"Can't validate backup data: {humanize_error(data, err)}",
                _LOGGER.error,
            ) from err

        # Restore metadata
        for attr in (
            ATTR_AUDIO_INPUT,
            ATTR_AUDIO_OUTPUT,
            ATTR_PORT,
            ATTR_SSL,
            ATTR_REFRESH_TOKEN,
            ATTR_WATCHDOG,
        ):
            if attr in data:
                self._data[attr] = data[attr]

    async def list_users(self) -> list[MuthurCommandUser]:
        """Fetch list of all users from Muthur Command Core via WebSocket.

        Raises MuthurCommandWSError on WebSocket connection/communication failure.
        """
        raw: list[dict[str, Any]] = await self.websocket.async_send_command(
            {ATTR_TYPE: "config/auth/list"}
        )
        return [MuthurCommandUser.from_dict(data) for data in raw]
