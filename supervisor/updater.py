"""Fetch last versions from webserver."""

import asyncio
from datetime import timedelta
import json
import logging

import aiohttp
from awesomeversion import AwesomeVersion

from .bus import EventListener
from .const import (
    ATTR_AUDIO,
    ATTR_AUTO_UPDATE,
    ATTR_CHANNEL,
    ATTR_CLI,
    ATTR_DNS,
    ATTR_IMAGE,
    ATTR_MC_BD,
    ATTR_MC_FD,
    ATTR_MCOS_UNRESTRICTED,
    ATTR_MCOS_UPGRADE,
    ATTR_MULTICAST,
    ATTR_MUTHURCOMMAND,
    ATTR_OBSERVER,
    ATTR_OTA,
    ATTR_POSTGRESQL,
    ATTR_REDIS,
    ATTR_SUPERVISOR,
    FILE_MCOS_UPDATER,
    URL_MCOS_VERSION,
    BusEvent,
    UpdateChannel,
)
from .coresys import CoreSys, CoreSysAttributes
from .docker.const import ContainerState
from .docker.monitor import DockerContainerStateEvent
from .exceptions import UpdaterError, UpdaterJobError
from .jobs.const import JobConcurrency, JobThrottle
from .jobs.decorator import Job, JobCondition
from .utils.common import FileConfiguration
from .utils.version_image_template import format_version_image_template
from .validate import SCHEMA_UPDATER_CONFIG

_LOGGER: logging.Logger = logging.getLogger(__name__)


class Updater(FileConfiguration, CoreSysAttributes):
    """Fetch last versions from version.json."""

    def __init__(self, coresys: CoreSys) -> None:
        """Initialize updater."""
        super().__init__(FILE_MCOS_UPDATER, SCHEMA_UPDATER_CONFIG)
        self.coresys = coresys
        self._connectivity_listener: EventListener | None = None
        self._dns_retry_listener: EventListener | None = None
        self._fetch_retry_pending: bool = False

    async def load(self) -> None:
        """Update internal data."""
        # Delay loading data by default so JobCondition.OS_SUPPORTED works.
        # Use MCOS unrestricted as indicator as this is what we need to evaluate
        # if the operating system version is supported.
        if self.sys_os.board and self.version_mcos_unrestricted is None:
            _LOGGER.info(
                "No OS update information in updater cache, "
                "will refresh after DNS is ready"
            )
            self._schedule_fetch_retry()

    async def reload(self) -> None:
        """Update internal data."""
        # If there's no connectivity, delay initial version fetch
        if not self.sys_supervisor.connectivity:
            _LOGGER.info("No Supervisor connectivity, delaying version fetch")
            self._schedule_fetch_retry()
            return

        try:
            await self.fetch_data()
            self._fetch_retry_pending = False
            self._clear_retry_listeners()
        except UpdaterError:
            _LOGGER.warning("Version fetch failed, will retry when DNS is ready")
            self._schedule_fetch_retry()

    @property
    def version_muthurcommand(self) -> AwesomeVersion | None:
        """Return latest version of Muthur Command Core."""
        return self._data.get(ATTR_MUTHURCOMMAND)

    @property
    def version_supervisor(self) -> AwesomeVersion | None:
        """Return latest version of Supervisor."""
        return self._data.get(ATTR_SUPERVISOR)

    @property
    def version_mcos(self) -> AwesomeVersion | None:
        """Return latest version of MCOS."""
        upgrade_map = self.upgrade_map_mcos
        unrestricted = self.version_mcos_unrestricted

        # If no upgrade map exists, fall back to unrestricted version
        if not upgrade_map:
            return unrestricted

        # If we have no unrestricted version or no current OS version, return unrestricted
        if (
            not unrestricted
            or not self.sys_os.version
            or self.sys_os.version.major is None
        ):
            return unrestricted

        current_major = str(self.sys_os.version.major)
        # Check if there's an upgrade path for current major version
        if current_major in upgrade_map:
            last_in_major = AwesomeVersion(upgrade_map[current_major])
            # If we're not at the last version in our major, upgrade to that first
            if self.sys_os.version != last_in_major:
                return last_in_major
            # If we are at the last version in our major, check for next major
            next_major = str(int(self.sys_os.version.major) + 1)
            if next_major in upgrade_map:
                return AwesomeVersion(upgrade_map[next_major])

        # Fall back to unrestricted version
        return unrestricted

    @property
    def version_mcos_unrestricted(self) -> AwesomeVersion | None:
        """Return latest version of MCOS ignoring upgrade restrictions."""
        return self._data.get(ATTR_MCOS_UNRESTRICTED)

    @property
    def upgrade_map_mcos(self) -> dict[str, str] | None:
        """Return MCOS upgrade map."""
        return self._data.get(ATTR_MCOS_UPGRADE)

    @property
    def version_cli(self) -> AwesomeVersion | None:
        """Return latest version of CLI."""
        return self._data.get(ATTR_CLI)

    @property
    def version_dns(self) -> AwesomeVersion | None:
        """Return latest version of DNS."""
        return self._data.get(ATTR_DNS)

    @property
    def version_audio(self) -> AwesomeVersion | None:
        """Return latest version of Audio."""
        return self._data.get(ATTR_AUDIO)

    @property
    def version_observer(self) -> AwesomeVersion | None:
        """Return latest version of Observer."""
        return self._data.get(ATTR_OBSERVER)

    @property
    def version_multicast(self) -> AwesomeVersion | None:
        """Return latest version of Multicast."""
        return self._data.get(ATTR_MULTICAST)

    @property
    def version_mc_bd(self) -> AwesomeVersion | None:
        """Return latest version of mc_bd (MC backend API)."""
        return self._data.get(ATTR_MC_BD)

    @property
    def version_mc_fd(self) -> AwesomeVersion | None:
        """Return latest version of mc_fd (MC frontend)."""
        return self._data.get(ATTR_MC_FD)

    @property
    def version_postgresql(self) -> AwesomeVersion | None:
        """Return latest version tag for PostgreSQL stack image."""
        return self._data.get(ATTR_POSTGRESQL)

    @property
    def version_redis(self) -> AwesomeVersion | None:
        """Return latest version tag for Redis stack image."""
        return self._data.get(ATTR_REDIS)

    @property
    def image_muthurcommand(self) -> str | None:
        """Return image of Muthur Command Core docker."""
        if ATTR_MUTHURCOMMAND not in self._data[ATTR_IMAGE]:
            return None
        return format_version_image_template(
            self._data[ATTR_IMAGE][ATTR_MUTHURCOMMAND],
            arch=self.sys_arch.supervisor,
            machine=self.sys_machine,
        )

    @property
    def image_supervisor(self) -> str | None:
        """Return image of Supervisor docker."""
        if ATTR_SUPERVISOR not in self._data[ATTR_IMAGE]:
            return None
        return self._data[ATTR_IMAGE][ATTR_SUPERVISOR].format(
            arch=self.sys_arch.supervisor
        )

    @property
    def image_cli(self) -> str | None:
        """Return image of CLI docker."""
        if ATTR_CLI not in self._data[ATTR_IMAGE]:
            return None
        return self._data[ATTR_IMAGE][ATTR_CLI].format(arch=self.sys_arch.supervisor)

    @property
    def image_dns(self) -> str | None:
        """Return image of DNS docker."""
        if ATTR_DNS not in self._data[ATTR_IMAGE]:
            return None
        return self._data[ATTR_IMAGE][ATTR_DNS].format(arch=self.sys_arch.supervisor)

    @property
    def image_audio(self) -> str | None:
        """Return image of Audio docker."""
        if ATTR_AUDIO not in self._data[ATTR_IMAGE]:
            return None
        return self._data[ATTR_IMAGE][ATTR_AUDIO].format(arch=self.sys_arch.supervisor)

    @property
    def image_observer(self) -> str | None:
        """Return image of Observer docker."""
        if ATTR_OBSERVER not in self._data[ATTR_IMAGE]:
            return None
        return self._data[ATTR_IMAGE][ATTR_OBSERVER].format(
            arch=self.sys_arch.supervisor
        )

    @property
    def image_multicast(self) -> str | None:
        """Return image of Multicast docker."""
        if ATTR_MULTICAST not in self._data[ATTR_IMAGE]:
            return None
        return self._data[ATTR_IMAGE][ATTR_MULTICAST].format(
            arch=self.sys_arch.supervisor
        )

    @property
    def image_mc_bd(self) -> str | None:
        """Return resolved image name (no tag) for mc_bd."""
        if ATTR_MC_BD not in self._data[ATTR_IMAGE]:
            return None
        return format_version_image_template(
            self._data[ATTR_IMAGE][ATTR_MC_BD],
            arch=self.sys_arch.supervisor,
            machine=self.sys_machine,
        )

    @property
    def image_mc_fd(self) -> str | None:
        """Return resolved image name (no tag) for mc_fd."""
        if ATTR_MC_FD not in self._data[ATTR_IMAGE]:
            return None
        return format_version_image_template(
            self._data[ATTR_IMAGE][ATTR_MC_FD],
            arch=self.sys_arch.supervisor,
            machine=self.sys_machine,
        )

    @property
    def image_postgresql(self) -> str | None:
        """Return resolved image name (no tag) for PostgreSQL."""
        if ATTR_POSTGRESQL not in self._data[ATTR_IMAGE]:
            return None
        return format_version_image_template(
            self._data[ATTR_IMAGE][ATTR_POSTGRESQL],
            arch=self.sys_arch.supervisor,
            machine=self.sys_machine,
        )

    @property
    def image_redis(self) -> str | None:
        """Return resolved image name (no tag) for Redis."""
        if ATTR_REDIS not in self._data[ATTR_IMAGE]:
            return None
        return format_version_image_template(
            self._data[ATTR_IMAGE][ATTR_REDIS],
            arch=self.sys_arch.supervisor,
            machine=self.sys_machine,
        )

    @property
    def ota_url(self) -> str | None:
        """Return OTA url for OS."""
        return self._data.get(ATTR_OTA)

    @property
    def channel(self) -> UpdateChannel:
        """Return upstream channel of Supervisor instance."""
        return self._data[ATTR_CHANNEL]

    @channel.setter
    def channel(self, value: UpdateChannel):
        """Set upstream mode."""
        self._data[ATTR_CHANNEL] = value

    @property
    def auto_update(self) -> bool:
        """Return if Supervisor auto updates enabled."""
        return self._data[ATTR_AUTO_UPDATE]

    @auto_update.setter
    def auto_update(self, value: bool) -> None:
        """Set Supervisor auto updates enabled."""
        self._data[ATTR_AUTO_UPDATE] = value

    async def _check_connectivity(self, connectivity: bool) -> None:
        """Fetch data once connectivity is true."""
        if connectivity:
            await self.reload()

    def _schedule_fetch_retry(self) -> None:
        """Register retry hooks and attempt fetch once DNS is running."""
        was_pending = self._fetch_retry_pending
        self._fetch_retry_pending = True
        self._ensure_retry_listeners()
        if not was_pending:
            self.sys_create_task(self._try_fetch_when_dns_ready())

    def _ensure_retry_listeners(self) -> None:
        """Listen for connectivity and DNS container start to retry version fetch."""
        if not self._connectivity_listener:
            self._connectivity_listener = self.sys_bus.register_event(
                BusEvent.SUPERVISOR_CONNECTIVITY_CHANGE, self._check_connectivity
            )
        if not self._dns_retry_listener:
            self._dns_retry_listener = self.sys_bus.register_event(
                BusEvent.DOCKER_CONTAINER_STATE_CHANGE,
                self._on_dns_container_state_for_fetch,
            )

    def _clear_retry_listeners(self) -> None:
        """Remove pending-fetch listeners after a successful version fetch."""
        if self._connectivity_listener:
            self.sys_bus.remove_listener(self._connectivity_listener)
            self._connectivity_listener = None
        if self._dns_retry_listener:
            self.sys_bus.remove_listener(self._dns_retry_listener)
            self._dns_retry_listener = None

    async def _on_dns_container_state_for_fetch(
        self, event: DockerContainerStateEvent
    ) -> None:
        """Retry version fetch when the DNS plugin container becomes healthy."""
        if not self._fetch_retry_pending:
            return
        if event.name != self.sys_plugins.dns.instance.name:
            return
        if event.state != ContainerState.RUNNING:
            return

        # Brief pause so CoreDNS accepts queries before we hit version.muthur-command.com
        await asyncio.sleep(2)
        await self._try_fetch_when_dns_ready()

    async def _try_fetch_when_dns_ready(self) -> None:
        """Attempt an online version fetch after DNS and connectivity are available."""
        if not self._fetch_retry_pending:
            return
        if not self.sys_supervisor.connectivity:
            return
        if not await self.sys_plugins.dns.is_running():
            return

        await self.coresys.init_websession()
        await self.reload()

    @Job(
        name="updater_fetch_data",
        conditions=[
            JobCondition.ARCHITECTURE_SUPPORTED,
            JobCondition.INTERNET_SYSTEM,
            JobCondition.OS_SUPPORTED,
        ],
        on_condition=UpdaterJobError,
        throttle_period=timedelta(seconds=30),
        concurrency=JobConcurrency.QUEUE,
        throttle=JobThrottle.THROTTLE,
    )
    async def fetch_data(self):
        """Fetch current versions from Github.

        Is a coroutine.
        """
        url = URL_MCOS_VERSION.format(channel=self.channel)
        machine = self.sys_machine or "default"

        # Get data
        try:
            _LOGGER.info("Fetching update data from %s", url)
            timeout = aiohttp.ClientTimeout(total=10)
            async with self.sys_websession.get(url, timeout=timeout) as request:
                if request.status != 200:
                    raise UpdaterError(
                        f"Fetching version from {url} response with {request.status}",
                        _LOGGER.warning,
                    )
                data = await request.read()

        except (aiohttp.ClientError, TimeoutError) as err:
            self.sys_supervisor.connectivity = False
            raise UpdaterError(
                f"Can't fetch versions from {url}: {str(err) or 'Timeout'}",
                _LOGGER.warning,
            ) from err

        # Fetch was successful — drop any pending-fetch listeners.
        self._fetch_retry_pending = False
        self._clear_retry_listeners()

        # Parse data
        try:
            data = json.loads(data)
        except json.JSONDecodeError as err:
            raise UpdaterError(
                f"Can't parse versions from {url}: {err}", _LOGGER.warning
            ) from err

        # data valid?
        if not data or data.get(ATTR_CHANNEL) != self.channel:
            raise UpdaterError(f"Invalid data from {url}", _LOGGER.warning)

        events = ["supervisor"]
        try:
            # Update supervisor version
            self._data[ATTR_SUPERVISOR] = AwesomeVersion(data["supervisor"])

            # Update muthurcommand (Core slot) version; "unused" or absent skips Core
            mc_map = data.get("muthurcommand") or {}
            mc_slot = mc_map.get(machine, mc_map.get("default"))
            if mc_slot is None or str(mc_slot).lower() == "unused":
                self._data[ATTR_MUTHURCOMMAND] = None
                self._data[ATTR_IMAGE].pop(ATTR_MUTHURCOMMAND, None)
            else:
                self._data[ATTR_MUTHURCOMMAND] = AwesomeVersion(mc_slot)
                events.append("muthurcommand")

            # Update Muthur Command OS version
            if self.sys_os.board:
                self._data[ATTR_OTA] = data["ota"]
                if version := data["mcos"].get(self.sys_os.board):
                    self._data[ATTR_MCOS_UNRESTRICTED] = AwesomeVersion(version)
                    # Store the upgrade map for persistent access
                    self._data[ATTR_MCOS_UPGRADE] = data.get("mcos_upgrade", {})
                    events.append("os")
                else:
                    _LOGGER.warning(
                        "Board '%s' not found in version file. No OS updates.",
                        self.sys_os.board,
                    )

            # Update MCOS plugins
            self._data[ATTR_CLI] = AwesomeVersion(data["cli"])
            self._data[ATTR_DNS] = AwesomeVersion(data["dns"])
            self._data[ATTR_AUDIO] = AwesomeVersion(data["audio"])
            self._data[ATTR_OBSERVER] = AwesomeVersion(data["observer"])
            self._data[ATTR_MULTICAST] = AwesomeVersion(data["multicast"])

            # Update images for that versions
            images = data["images"]
            if self._data.get(ATTR_MUTHURCOMMAND) is not None:
                if not (mc_img := images.get("muthurcommand") or images.get("mc_bd")):
                    raise UpdaterError(
                        "Version data missing muthurcommand image template",
                        _LOGGER.warning,
                    )
                self._data[ATTR_IMAGE][ATTR_MUTHURCOMMAND] = mc_img
            self._data[ATTR_IMAGE][ATTR_SUPERVISOR] = images["supervisor"]
            self._data[ATTR_IMAGE][ATTR_AUDIO] = images["audio"]
            self._data[ATTR_IMAGE][ATTR_CLI] = images["cli"]
            self._data[ATTR_IMAGE][ATTR_DNS] = images["dns"]
            self._data[ATTR_IMAGE][ATTR_OBSERVER] = images["observer"]
            self._data[ATTR_IMAGE][ATTR_MULTICAST] = images["multicast"]

            for key in (ATTR_MC_BD, ATTR_MC_FD, ATTR_POSTGRESQL, ATTR_REDIS):
                if key in images:
                    self._data[ATTR_IMAGE][key] = images[key]
                else:
                    self._data[ATTR_IMAGE].pop(key, None)

            for key in (ATTR_MC_BD, ATTR_MC_FD, ATTR_POSTGRESQL, ATTR_REDIS):
                if key in data:
                    self._data[key] = AwesomeVersion(data[key])
                else:
                    self._data.pop(key, None)

        except KeyError as err:
            raise UpdaterError(
                f"Can't process version data: {err}", _LOGGER.warning
            ) from err

        await self.save_data()

        # Send status update to core
        for event in events:
            self.sys_muthurcommand.websocket.supervisor_update_event(event)
