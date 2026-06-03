"""A collection of tasks."""

from contextlib import suppress
from datetime import datetime, timedelta
import logging
from typing import cast

from ..addons.const import ADDON_UPDATE_CONDITIONS
from ..backups.const import LOCATION_CLOUD_BACKUP, LOCATION_TYPE
from ..const import ATTR_TYPE, AddonState
from ..coresys import CoreSysAttributes
from ..docker.const import ContainerState
from ..exceptions import (
    AddonsError,
    BackupFileNotFoundError,
    DockerError,
    MCStackError,
    MuthurCommandError,
    MuthurCommandWSError,
    ObserverError,
    SupervisorUpdateError,
)
from ..jobs.const import JobConcurrency
from ..jobs.decorator import Job, JobCondition
from ..muthurcommand.const import WSType, is_landingpage
from ..plugins.const import PLUGIN_UPDATE_CONDITIONS
from ..utils.dt import utcnow
from ..utils.sentry import async_capture_exception

_LOGGER: logging.Logger = logging.getLogger(__name__)

HASS_WATCHDOG_API_FAILURES = "HASS_WATCHDOG_API_FAILURES"
HASS_WATCHDOG_REANIMATE_FAILURES = "HASS_WATCHDOG_REANIMATE_FAILURES"
HASS_WATCHDOG_MAX_API_ATTEMPTS = 2
HASS_WATCHDOG_MAX_REANIMATE_ATTEMPTS = 5

RUN_UPDATE_ADDONS = 57600
RUN_UPDATE_CLI = 43200  # 12h, staggered +2min per plugin
RUN_UPDATE_DNS = 43320
RUN_UPDATE_AUDIO = 43440
RUN_UPDATE_MULTICAST = 43560
RUN_UPDATE_OBSERVER = 43680

RUN_RELOAD_ADDONS = 10800
RUN_RELOAD_BACKUPS = 72000
RUN_RELOAD_HOST = 7600
RUN_RELOAD_UPDATER = 86400  # 24h
RUN_RELOAD_INGRESS = 930
RUN_RELOAD_MOUNTS = 900

RUN_WATCHDOG_MUTHURCOMMAND_API = 120

RUN_WATCHDOG_ADDON_APPLICATON = 120
RUN_WATCHDOG_OBSERVER_APPLICATION = 180
RUN_WATCHDOG_MC_STACK = 120

RUN_CORE_BACKUP_CLEANUP = 86200

# MC stack watchdog cache keys
MC_STACK_WATCHDOG_API_FAILURES = "MC_STACK_WATCHDOG_API_FAILURES"
MC_STACK_WATCHDOG_MAX_API_ATTEMPTS = 2

PLUGIN_AUTO_UPDATE_CONDITIONS = PLUGIN_UPDATE_CONDITIONS + [
    JobCondition.AUTO_UPDATE,
    JobCondition.RUNNING,
]

OLD_BACKUP_THRESHOLD = timedelta(days=2)


class Tasks(CoreSysAttributes):
    """Handle Tasks inside Supervisor."""

    def __init__(self, coresys):
        """Initialize Tasks."""
        self.coresys = coresys
        self._cache = {}

    async def load(self):
        """Add Tasks to scheduler."""
        # Update
        self.sys_scheduler.register_task(self._update_addons, RUN_UPDATE_ADDONS)
        self.sys_scheduler.register_task(self._update_cli, RUN_UPDATE_CLI)
        self.sys_scheduler.register_task(self._update_dns, RUN_UPDATE_DNS)
        self.sys_scheduler.register_task(self._update_audio, RUN_UPDATE_AUDIO)
        self.sys_scheduler.register_task(self._update_multicast, RUN_UPDATE_MULTICAST)
        self.sys_scheduler.register_task(self._update_observer, RUN_UPDATE_OBSERVER)

        # Reload
        self.sys_scheduler.register_task(self._reload_store, RUN_RELOAD_ADDONS)
        self.sys_scheduler.register_task(self._reload_updater, RUN_RELOAD_UPDATER)
        self.sys_scheduler.register_task(self.sys_backups.reload, RUN_RELOAD_BACKUPS)
        self.sys_scheduler.register_task(self.sys_host.reload, RUN_RELOAD_HOST)
        self.sys_scheduler.register_task(self.sys_ingress.reload, RUN_RELOAD_INGRESS)
        self.sys_scheduler.register_task(self.sys_mounts.reload, RUN_RELOAD_MOUNTS)

        # Watchdog
        self.sys_scheduler.register_task(
            self._watchdog_muthurcommand_api, RUN_WATCHDOG_MUTHURCOMMAND_API
        )
        self.sys_scheduler.register_task(
            self._watchdog_observer_application, RUN_WATCHDOG_OBSERVER_APPLICATION
        )
        self.sys_scheduler.register_task(
            self._watchdog_addon_application, RUN_WATCHDOG_ADDON_APPLICATON
        )
        self.sys_scheduler.register_task(self._watchdog_mc_stack, RUN_WATCHDOG_MC_STACK)

        # Cleanup
        self.sys_scheduler.register_task(
            self._core_backup_cleanup, RUN_CORE_BACKUP_CLEANUP
        )

        _LOGGER.info("All core tasks are scheduled")

    @Job(
        name="tasks_update_addons",
        conditions=ADDON_UPDATE_CONDITIONS + [JobCondition.RUNNING],
    )
    async def _update_addons(self):
        """Check if an update is available for an Add-on and update it."""
        for addon in self.sys_addons.all:
            if not addon.is_installed or not addon.auto_update:
                continue

            # Evaluate available updates
            if not addon.need_update:
                continue
            if not addon.auto_update_available:
                _LOGGER.debug(
                    "Not updating app %s from %s to %s as that would cross a known breaking version",
                    addon.slug,
                    addon.version,
                    addon.latest_version,
                )
                continue
            # Delay auto-updates for a day in case of issues
            if utcnow() < addon.latest_version_timestamp + timedelta(days=1):
                _LOGGER.debug(
                    "Not updating app %s from %s to %s as the latest version is less than a day old",
                    addon.slug,
                    addon.version,
                    addon.latest_version,
                )
                continue
            if not addon.test_update_schema():
                _LOGGER.warning(
                    "App %s will be ignored, schema tests failed", addon.slug
                )
                continue

            _LOGGER.info("App auto update process %s", addon.slug)
            # Call Muthur Command Core to update add-on to make sure that backups
            # get created through the Muthur Command Core API (categorized correctly).
            # Ultimately auto updates should be handled by Muthur Command Core itself
            # through a update entity feature.
            message = {
                ATTR_TYPE: WSType.MCOS_UPDATE_ADDON,
                "addon": addon.slug,
                "backup": True,
            }
            _LOGGER.debug(
                "Sending update app WebSocket command to Muthur Command Core: %s",
                message,
            )
            try:
                await self.sys_muthurcommand.websocket.async_send_command(message)
            except MuthurCommandWSError as err:
                _LOGGER.warning(
                    "Could not send app update command to Muthur Command Core: %s",
                    err,
                )

    async def _watchdog_muthurcommand_api(self):
        """Create scheduler task for monitoring running state of API.

        Try 2 times to call API before we restart Home-Assistant. Maybe we had
        a delay in our system.
        """
        if self.sys_muthurcommand.unused:
            # MCOS variant doesn't ship Muthur Command Core — Stage 5 of
            # the A1 plan tells us the MC stack watchdog (mc_bd HTTP probe)
            # is the source of truth instead.
            return
        if not self.sys_muthurcommand.watchdog:
            # Watchdog is not enabled for Muthur Command
            return
        if self.sys_muthurcommand.error_state:
            # Muthur Command is in an error state, this is handled by the rollback feature
            return
        if is_landingpage(self.sys_muthurcommand.version):
            # Skip watchdog for landingpage
            return
        if not await self.sys_muthurcommand.core.is_running():
            # The home assistant container is not running
            return
        if self.sys_muthurcommand.core.in_progress:
            # Muthur Command has a task in progress
            return
        if await self.sys_muthurcommand.api.check_api_state():
            # Muthur Command is running properly
            self._cache[HASS_WATCHDOG_REANIMATE_FAILURES] = 0
            self._cache[HASS_WATCHDOG_API_FAILURES] = 0
            return

        # Init cache data
        api_fails = self._cache.get(HASS_WATCHDOG_API_FAILURES, 0)

        # Look like we run into a problem
        api_fails += 1
        if api_fails < HASS_WATCHDOG_MAX_API_ATTEMPTS:
            self._cache[HASS_WATCHDOG_API_FAILURES] = api_fails
            _LOGGER.warning("Watchdog missed a Muthur Command Core API response.")
            return

        # After 5 reanimation attempts switch to safe mode. If that fails, give up
        reanimate_fails = self._cache.get(HASS_WATCHDOG_REANIMATE_FAILURES, 0)
        if reanimate_fails > HASS_WATCHDOG_MAX_REANIMATE_ATTEMPTS:
            return

        if safe_mode := reanimate_fails == HASS_WATCHDOG_MAX_REANIMATE_ATTEMPTS:
            _LOGGER.critical(
                "Watchdog cannot reanimate Muthur Command Core, failed all %s attempts. Restarting into safe mode",
                reanimate_fails,
            )
        else:
            _LOGGER.error(
                "Watchdog missed %s Muthur Command Core API responses in a row. Restarting Muthur Command Core!",
                HASS_WATCHDOG_MAX_API_ATTEMPTS,
            )

        try:
            if safe_mode:
                await self.sys_muthurcommand.core.rebuild(safe_mode=True)
            else:
                await self.sys_muthurcommand.core.restart()
        except MuthurCommandError as err:
            if reanimate_fails == 0 or safe_mode:
                await async_capture_exception(err)

            if safe_mode:
                _LOGGER.critical(
                    "Safe mode restart failed. Watchdog cannot bring Muthur Command online."
                )
            else:
                _LOGGER.error("Muthur Command watchdog reanimation failed!")

            self._cache[HASS_WATCHDOG_REANIMATE_FAILURES] = reanimate_fails + 1
        else:
            self._cache[HASS_WATCHDOG_REANIMATE_FAILURES] = 0
        finally:
            self._cache[HASS_WATCHDOG_API_FAILURES] = 0

    @Job(name="tasks_update_cli", conditions=PLUGIN_AUTO_UPDATE_CONDITIONS)
    async def _update_cli(self):
        """Check and run update of cli."""
        if not self.sys_plugins.cli.need_update:
            return

        _LOGGER.info(
            "Found new cli version %s, updating", self.sys_plugins.cli.latest_version
        )
        await self.sys_plugins.cli.update()

    @Job(name="tasks_update_dns", conditions=PLUGIN_AUTO_UPDATE_CONDITIONS)
    async def _update_dns(self):
        """Check and run update of CoreDNS plugin."""
        if not self.sys_plugins.dns.need_update:
            return

        _LOGGER.info(
            "Found new CoreDNS plugin version %s, updating",
            self.sys_plugins.dns.latest_version,
        )
        await self.sys_plugins.dns.update()

    @Job(name="tasks_update_audio", conditions=PLUGIN_AUTO_UPDATE_CONDITIONS)
    async def _update_audio(self):
        """Check and run update of PulseAudio plugin."""
        if not self.sys_plugins.audio.need_update:
            return

        _LOGGER.info(
            "Found new PulseAudio plugin version %s, updating",
            self.sys_plugins.audio.latest_version,
        )
        await self.sys_plugins.audio.update()

    @Job(name="tasks_update_observer", conditions=PLUGIN_AUTO_UPDATE_CONDITIONS)
    async def _update_observer(self):
        """Check and run update of Observer plugin."""
        if not self.sys_plugins.observer.need_update:
            return

        _LOGGER.info(
            "Found new Observer plugin version %s, updating",
            self.sys_plugins.observer.latest_version,
        )
        await self.sys_plugins.observer.update()

    @Job(name="tasks_update_multicast", conditions=PLUGIN_AUTO_UPDATE_CONDITIONS)
    async def _update_multicast(self):
        """Check and run update of multicast."""
        if not self.sys_plugins.multicast.need_update:
            return

        _LOGGER.info(
            "Found new Multicast version %s, updating",
            self.sys_plugins.multicast.latest_version,
        )
        await self.sys_plugins.multicast.update()

    async def _watchdog_observer_application(self):
        """Check running state of application and rebuild if they is not response."""
        # if observer plugin is active
        if (
            self.sys_plugins.observer.in_progress
            or await self.sys_plugins.observer.check_system_runtime()
        ):
            return
        _LOGGER.warning("Watchdog/Application found a problem with observer plugin!")

        try:
            await self.sys_plugins.observer.rebuild()
        except ObserverError:
            _LOGGER.error("Observer watchdog reanimation failed!")

    async def _watchdog_addon_application(self):
        """Check running state of the application and start if they is hangs."""
        for addon in self.sys_addons.installed:
            # if watchdog need looking for
            if not addon.watchdog or addon.state != AddonState.STARTED:
                continue

            # Init cache data
            retry_scan = self._cache.get(addon.slug, 0)

            # if Addon have running actions / Application work
            if addon.in_progress or await addon.watchdog_application():
                continue

            # Look like we run into a problem
            retry_scan += 1
            if retry_scan == 1:
                self._cache[addon.slug] = retry_scan
                _LOGGER.warning(
                    "Watchdog missing application response from %s", addon.slug
                )
                return

            _LOGGER.warning("Watchdog found a problem with %s application!", addon.slug)
            try:
                await (await addon.restart())
            except AddonsError as err:
                _LOGGER.error("%s watchdog reanimation failed with %s", addon.slug, err)
                await async_capture_exception(err)
            finally:
                self._cache[addon.slug] = 0

    @Job(
        name="tasks_reload_store",
        conditions=[
            JobCondition.SUPERVISOR_UPDATED,
            JobCondition.OS_SUPPORTED,
            JobCondition.MUTHURCOMMAND_CORE_SUPPORTED,
        ],
    )
    async def _reload_store(self) -> None:
        """Reload store and check for addon updates."""
        await self.sys_store.reload()

    @Job(name="tasks_reload_updater")
    async def _reload_updater(self) -> None:
        """Check for new versions of Muthur Command, Supervisor, OS, etc."""
        await self.sys_updater.reload()

        # If there's a new version of supervisor, update immediately
        if self.sys_supervisor.need_update:
            await self._auto_update_supervisor()

    @Job(
        name="tasks_update_supervisor",
        conditions=[
            JobCondition.AUTO_UPDATE,
            JobCondition.FREE_SPACE,
            JobCondition.HEALTHY,
            JobCondition.INTERNET_HOST,
            JobCondition.OS_SUPPORTED,
            JobCondition.RUNNING,
            JobCondition.ARCHITECTURE_SUPPORTED,
        ],
        concurrency=JobConcurrency.REJECT,
    )
    async def _auto_update_supervisor(self):
        """Auto update Supervisor if enabled."""
        if not self.sys_supervisor.need_update:
            return

        _LOGGER.info(
            "Found new Supervisor version %s, updating",
            self.sys_supervisor.latest_version,
        )
        with suppress(SupervisorUpdateError):
            await self.sys_supervisor.update()

    async def _watchdog_mc_stack(self) -> None:
        """Watch mc_bd HTTP health and revive crashed stack containers.

        Recovery policy (A1 plan, stage 5):

        1. Backend ``mc_bd`` HTTP probe must answer within
           ``MC_STACK_WATCHDOG_MAX_API_ATTEMPTS`` polls. A single missed
           probe is forgiven; two in a row trigger recovery.
        2. **First**: restart only the ``mc_bd`` container — that's the
           cheapest fix and the most common failure mode.
        3. **Then**: if ``mc_bd`` is actually crashed (``FAILED`` /
           ``STOPPED``), do a full ordered stack restart. PostgreSQL and
           Redis containers are recreated, but their bind-mount data
           volumes are *never* removed by this method.
        4. **Never**: touch persistent data without explicit user action.
        """
        stack = self.sys_mc_stack
        if not stack.enabled:
            return
        if not stack.watchdog:
            # Operator has switched the stack watchdog off (e.g. during
            # planned maintenance). Reset the failure counter so a future
            # re-enable starts from a clean slate.
            self._cache[MC_STACK_WATCHDOG_API_FAILURES] = 0
            return

        backend = stack.backend
        if not await backend.is_running():
            # Container hasn't been brought up yet (maybe still pulling on
            # first boot). Don't compete with ``MCStack.start`` here.
            return

        # HTTP probe — same code path the start-up health check uses.
        if await stack._check_backend_ready():  # noqa: SLF001  # pylint: disable=protected-access
            if self._cache.get(MC_STACK_WATCHDOG_API_FAILURES):
                _LOGGER.info("MC stack watchdog: mc_bd recovered")
            self._cache[MC_STACK_WATCHDOG_API_FAILURES] = 0
            return

        api_fails = self._cache.get(MC_STACK_WATCHDOG_API_FAILURES, 0) + 1
        if api_fails < MC_STACK_WATCHDOG_MAX_API_ATTEMPTS:
            self._cache[MC_STACK_WATCHDOG_API_FAILURES] = api_fails
            _LOGGER.warning(
                "MC stack watchdog missed an mc_bd health response (%s/%s)",
                api_fails,
                MC_STACK_WATCHDOG_MAX_API_ATTEMPTS,
            )
            return

        # Reset failure counter so the next pass starts fresh after recovery.
        self._cache[MC_STACK_WATCHDOG_API_FAILURES] = 0
        _LOGGER.error(
            "MC stack watchdog: mc_bd unresponsive %s times, attempting recovery",
            MC_STACK_WATCHDOG_MAX_API_ATTEMPTS,
        )

        # Tier 1: container-only restart of mc_bd.
        try:
            await backend.restart()
        except DockerError as err:
            _LOGGER.warning("MC stack watchdog: mc_bd restart failed: %s", err)
        else:
            _LOGGER.info("MC stack watchdog: mc_bd restarted; re-probing on next tick")
            return

        # Tier 2: if mc_bd is actually dead, restart the whole stack
        # (postgres + redis are not destroyed, only restarted).
        if await backend.current_state() in (
            ContainerState.FAILED,
            ContainerState.STOPPED,
        ):
            _LOGGER.error(
                "MC stack watchdog: mc_bd is %s, restarting whole MC stack — "
                "PostgreSQL/Redis volumes are preserved",
                await backend.current_state(),
            )
            try:
                await stack.restart()
            except MCStackError as err:
                _LOGGER.error("MC stack watchdog: stack restart failed: %s", err)
                await async_capture_exception(err)

    @Job(name="tasks_core_backup_cleanup", conditions=[JobCondition.HEALTHY])
    async def _core_backup_cleanup(self) -> None:
        """Core backup is intended for transient use, remove any old backups that got left behind."""
        old_backups = [
            backup
            for backup in self.sys_backups.list_backups
            if LOCATION_CLOUD_BACKUP in backup.all_locations
            and datetime.fromisoformat(backup.date) < utcnow() - OLD_BACKUP_THRESHOLD
        ]
        for backup in old_backups:
            try:
                await self.sys_backups.remove(
                    backup, [cast(LOCATION_TYPE, LOCATION_CLOUD_BACKUP)]
                )
            except BackupFileNotFoundError as err:
                _LOGGER.debug("Can't remove backup %s: %s", backup.slug, err)
