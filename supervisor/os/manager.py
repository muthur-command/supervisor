"""OS support on supervisor."""

from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path, PurePath
from typing import cast

import aiohttp
from awesomeversion import AwesomeVersion, AwesomeVersionException
from cpe import CPE

from ..coresys import CoreSys, CoreSysAttributes
from ..dbus.agent.boards.const import BOARD_NAME_SUPERVISED
from ..dbus.rauc import RaucState, SlotStatusDataType
from ..exceptions import (
    DBusError,
    McosJobError,
    McosSlotNotFound,
    McosSlotUpdateError,
    McosUpdateError,
)
from ..jobs.const import JobConcurrency, JobCondition
from ..jobs.decorator import Job
from ..utils.sentry import async_capture_exception
from .data_disk import DataDisk

_LOGGER: logging.Logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class SlotStatus:
    """Status of a slot."""

    class_: str
    type_: str
    state: str
    device: PurePath
    bundle_compatible: str | None = None
    sha256: str | None = None
    size: int | None = None
    installed_count: int | None = None
    bundle_version: AwesomeVersion | None = None
    installed_timestamp: datetime | None = None
    status: str | None = None
    activated_count: int | None = None
    activated_timestamp: datetime | None = None
    boot_status: RaucState | None = None
    bootname: str | None = None
    parent: str | None = None

    @classmethod
    def from_dict(cls, data: SlotStatusDataType) -> SlotStatus:
        """Create SlotStatus from dictionary."""
        return cls(
            class_=data["class"],
            type_=data["type"],
            state=data["state"],
            device=PurePath(data["device"]),
            bundle_compatible=data.get("bundle.compatible"),
            sha256=data.get("sha256"),
            size=cast(int | None, data.get("size")),
            installed_count=cast(int | None, data.get("installed.count")),
            bundle_version=AwesomeVersion(data["bundle.version"])
            if "bundle.version" in data
            else None,
            installed_timestamp=datetime.fromisoformat(data["installed.timestamp"])
            if "installed.timestamp" in data
            else None,
            status=data.get("status"),
            activated_count=cast(int | None, data.get("activated.count")),
            activated_timestamp=datetime.fromisoformat(data["activated.timestamp"])
            if "activated.timestamp" in data
            else None,
            boot_status=RaucState(data["boot-status"])
            if "boot-status" in data
            else None,
            bootname=data.get("bootname"),
            parent=data.get("parent"),
        )


class OSManager(CoreSysAttributes):
    """OS interface inside supervisor."""

    def __init__(self, coresys: CoreSys):
        """Initialize MCOS handler."""
        self.coresys: CoreSys = coresys
        self._datadisk: DataDisk = DataDisk(coresys)
        self._available: bool = False
        self._version: AwesomeVersion | None = None
        self._board: str | None = None
        self._os_name: str | None = None
        self._slots: dict[str, SlotStatus] | None = None

    @property
    def available(self) -> bool:
        """Return True, if MCOS on host."""
        return self._available

    @property
    def version(self) -> AwesomeVersion | None:
        """Return version of MCOS."""
        return self._version

    @property
    def latest_version(self) -> AwesomeVersion | None:
        """Return version of MCOS."""
        return self.sys_updater.version_mcos

    @property
    def latest_version_unrestricted(self) -> AwesomeVersion | None:
        """Return current latest version of MCOS for board ignoring upgrade restrictions."""
        return self.sys_updater.version_mcos_unrestricted

    @property
    def need_update(self) -> bool:
        """Return true if a MCOS update is available."""
        try:
            return (
                self.version is not None
                and self.latest_version is not None
                and self.version < self.latest_version
            )
        except (AwesomeVersionException, TypeError):
            return False

    @property
    def board(self) -> str | None:
        """Return board name."""
        return self._board

    @property
    def os_name(self) -> str | None:
        """Return OS name."""
        return self._os_name

    @property
    def datadisk(self) -> DataDisk:
        """Return Operating-System datadisk."""
        return self._datadisk

    @property
    def slots(self) -> list[SlotStatus]:
        """Return status of slots."""
        if not self._slots:
            return []
        return list(self._slots.values())

    def get_slot_name(self, boot_name: str) -> str:
        """Get slot name from boot name."""
        if not self._slots:
            raise McosSlotNotFound()

        for name, status in self._slots.items():
            if status.bootname == boot_name:
                return name
        raise McosSlotNotFound()

    def _get_download_url(self, version: AwesomeVersion) -> str:
        raw_url = self.sys_updater.ota_url
        if raw_url is None:
            raise McosUpdateError("Don't have an URL for OTA updates!", _LOGGER.error)

        update_board = self.board
        update_os_name = "mcos"

        # OS version 6 and later renamed intel-nuc to generic-x86-64...
        if update_board == "intel-nuc" and version >= 6.0:
            update_board = "generic-x86-64"

        url = raw_url.format(
            version=str(version), board=update_board, os_name=update_os_name
        )
        return url

    async def _download_raucb(self, url: str, raucb: Path) -> None:
        """Download rauc bundle (OTA) from URL."""
        _LOGGER.info("Fetch OTA update from %s", url)
        try:
            timeout = aiohttp.ClientTimeout(total=60 * 60, connect=180)
            async with self.sys_websession.get(url, timeout=timeout) as request:
                if request.status != 200:
                    raise McosUpdateError(
                        f"Error raised from OTA Webserver: {request.status}",
                        _LOGGER.error,
                    )

                # Download RAUCB file
                ota_file = await self.sys_run_in_executor(raucb.open, "wb")
                try:
                    while True:
                        chunk = await request.content.read(1_048_576)
                        if not chunk:
                            break
                        await self.sys_run_in_executor(ota_file.write, chunk)
                finally:
                    await self.sys_run_in_executor(ota_file.close)

            _LOGGER.info("Completed download of OTA update file %s", raucb)

        except (aiohttp.ClientError, TimeoutError) as err:
            self.sys_supervisor.connectivity = False
            raise McosUpdateError(
                f"Can't fetch OTA update from {url}: {err!s}", _LOGGER.error
            ) from err

        except OSError as err:
            self.sys_resolution.check_oserror(err)
            raise McosUpdateError(
                f"Can't write OTA file: {err!s}", _LOGGER.error
            ) from err

    @Job(name="os_manager_reload", conditions=[JobCondition.MCOS], internal=True)
    async def reload(self) -> None:
        """Update cache of slot statuses."""
        if not self.sys_dbus.rauc.is_connected:
            _LOGGER.debug("Skipping RAUC slot reload: host has no rauc support")
            self._slots = {}
            return

        self._slots = {
            slot[0]: SlotStatus.from_dict(slot[1])
            for slot in await self.sys_dbus.rauc.get_slot_status()
        }

    async def load(self) -> None:
        """Load MCOS data."""
        try:
            if not self.sys_host.info.cpe:
                raise NotImplementedError()

            cpe = CPE(self.sys_host.info.cpe)
            os_name = cpe.get_product()[0]
            if os_name != "mcos":
                self._board = BOARD_NAME_SUPERVISED.lower()
                raise NotImplementedError()
        except NotImplementedError:
            _LOGGER.info("No Muthur Command OS found")
            return

        # Store meta data
        self._available = True
        self.sys_host.supported_features.cache_clear()
        self._version = AwesomeVersion(cpe.get_version()[0])
        self._board = cpe.get_target_hardware()[0]
        self._os_name = cpe.get_product()[0]

        if self.sys_dbus.rauc.is_connected:
            await self.reload()
        else:
            self._slots = {}

        await self.datadisk.load()

        boot_slot = (
            self.sys_dbus.rauc.boot_slot if self.sys_dbus.rauc.is_connected else "n/a"
        )
        _LOGGER.info(
            "Detect Muthur Command OS %s / BootSlot %s",
            self.version,
            boot_slot,
        )

    @Job(
        name="os_manager_config_sync",
        conditions=[JobCondition.MCOS],
        on_condition=McosJobError,
    )
    async def config_sync(self) -> None:
        """Trigger a host config reload from usb."""
        _LOGGER.info("Synchronizing configuration from USB with Muthur Command OS.")
        await self.sys_host.services.restart("mcos-config.service")

    @Job(
        name="os_manager_update",
        conditions=[
            JobCondition.MCOS,
            JobCondition.HEALTHY,
            JobCondition.INTERNET_SYSTEM,
            JobCondition.RUNNING,
            JobCondition.SUPERVISOR_UPDATED,
        ],
        on_condition=McosJobError,
        concurrency=JobConcurrency.REJECT,
    )
    async def update(self, version: AwesomeVersion | None = None) -> None:
        """Update MCOS system."""
        version = version or self.latest_version

        # Check installed version
        if not version:
            raise McosUpdateError(
                "No version information available, cannot update", _LOGGER.error
            )
        if version == self.version:
            raise McosUpdateError(
                f"Version {version!s} is already installed", _LOGGER.warning
            )

        # Fetch files from internet
        ota_url = self._get_download_url(version)
        int_ota = Path(self.sys_config.path_tmp, f"mcos-{version!s}.raucb")
        await self._download_raucb(ota_url, int_ota)
        ext_ota = Path(self.sys_config.path_extern_tmp, int_ota.name)

        try:
            async with self.sys_dbus.rauc.signal_completed() as signal:
                # Start listening for signals before triggering install
                # This prevents a race condition with install complete signal

                await self.sys_dbus.rauc.install(ext_ota)
                completed = await signal.wait_for_signal()

        except DBusError as err:
            raise McosUpdateError("Rauc communication error", _LOGGER.error) from err

        finally:
            int_ota.unlink()

        # Update success
        if 0 in completed:
            _LOGGER.info("Install of Muthur Command OS %s success", version)
            self.sys_create_task(self.sys_host.control.reboot())
            return

        # Update failed
        await self.sys_dbus.rauc.update()
        _LOGGER.error(
            "Muthur Command OS update failed with: %s",
            self.sys_dbus.rauc.last_error,
        )
        raise McosUpdateError()

    @Job(name="os_manager_mark_healthy", conditions=[JobCondition.MCOS], internal=True)
    async def mark_healthy(self) -> None:
        """Set booted partition as good for rauc."""
        if not self.sys_dbus.rauc.is_connected:
            _LOGGER.debug("Skipping RAUC mark healthy: host has no rauc support")
            return

        try:
            responses = [
                await self.sys_dbus.rauc.mark(RaucState.ACTIVE, "booted"),
                await self.sys_dbus.rauc.mark(RaucState.GOOD, "booted"),
            ]
        except DBusError:
            _LOGGER.exception("Can't mark booted partition as healthy!")
        else:
            _LOGGER.info(
                "Rauc: slot %s - %s, %s",
                self.sys_dbus.rauc.boot_slot,
                responses[0][1],
                responses[1][1],
            )
            await self.reload()

    @Job(
        name="os_manager_set_boot_slot",
        conditions=[JobCondition.MCOS],
        on_condition=McosJobError,
        internal=True,
    )
    async def set_boot_slot(self, boot_name: str) -> None:
        """Set active boot slot."""
        try:
            response = await self.sys_dbus.rauc.mark(
                RaucState.ACTIVE, self.get_slot_name(boot_name)
            )
        except DBusError as err:
            await async_capture_exception(err)
            raise McosSlotUpdateError(
                f"Can't mark {boot_name} as active!", _LOGGER.error
            ) from err

        _LOGGER.info("Rauc: %s - %s", self.sys_dbus.rauc.boot_slot, response[1])

        _LOGGER.info("Rebooting into new boot slot now")
        await self.sys_host.control.reboot()
