"""MC core application stack (PostgreSQL → Redis → mc_bd → mc_fd).

Stage 4 of the A1 plan: Supervisor manages the four core MC containers as
one unit. ``MCStack`` exposes ``load`` / ``start`` / ``stop`` / ``restart``
/ ``update`` / ``healthcheck`` and is wired into :class:`Core` so cold
boot, restart and upgrade go through the documented order.

Update policy (per A1 plan, see "升级策略" / "首启超时"):

* **mc_bd / mc_fd** are stateless containers — we *recreate* them in place
  by re-running ``DockerInterface.update`` (image pull → stop → start).
* **postgresql / redis** are stateful — we *only* swap the image tag and
  perform an ordered restart. Persistent volumes are never removed
  automatically (the plan explicitly forbids ``docker volume rm`` here).

A per-component **single timeout** plus a **stack-total timeout** prevent
a single hung container from blocking Supervisor startup.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from http import HTTPStatus
from ipaddress import IPv4Address
import logging
from typing import Any, Final

import aiodocker
import aiohttp
from awesomeversion import AwesomeVersion, AwesomeVersionException

from ..const import (
    MC_BACKEND_HEALTH_PATH,
    MC_BACKEND_PORT,
    MC_FRONTEND_PORT,
    MC_POSTGRES_DEFAULT_DB,
    MC_POSTGRES_DEFAULT_USER,
)
from ..coresys import CoreSys, CoreSysAttributes
from ..docker.const import ContainerState
from ..docker.interface import DockerInterface
from ..docker.mc_backend import DockerMcBackend
from ..docker.mc_frontend import DockerMcFrontend
from ..docker.mc_postgres import DockerMcPostgres
from ..docker.mc_redis import DockerMcRedis
from ..docker.mc_stack_base import (
    MC_BACKEND_DNS_ALIASES,
    MC_FRONTEND_DNS_ALIASES,
    MC_POSTGRES_DNS_ALIASES,
    MC_REDIS_DNS_ALIASES,
    mc_stack_alias_hosts,
    mc_stack_container_ip,
)
from ..exceptions import (
    CoreDNSError,
    DockerError,
    MCStackError,
    MCStackStartupError,
    MCStackUpdateError,
)
from ..muthurcommand.mc_stack_secrets import MCStackSecrets
from .mc_stack_config import MCStackConfig

_LOGGER: logging.Logger = logging.getLogger(__name__)

# Per-component startup timeouts. PostgreSQL gets the longest budget because
# initial DB initialization on a fresh data dir can take a while; mc_bd is
# next as it may run alembic migrations on first boot.
_TIMEOUT_POSTGRES: Final[timedelta] = timedelta(minutes=3)
_TIMEOUT_REDIS: Final[timedelta] = timedelta(seconds=60)
_TIMEOUT_MC_BD: Final[timedelta] = timedelta(minutes=5)
_TIMEOUT_MC_FD: Final[timedelta] = timedelta(seconds=60)

_HEALTH_POLL_SECONDS: Final[int] = 2

# Total upper bound for ``MCStack.start``. Exceeds the sum of per-step
# timeouts to leave room for image pull on first boot.
_TOTAL_START_TIMEOUT: Final[timedelta] = timedelta(minutes=15)


class MCStackUpdateStrategy(StrEnum):
    """Update strategy enum per A1 plan, stage 4 ‘升级策略’."""

    # mc_bd / mc_fd: pull new image, stop old container, start new one
    # (volumes are bind-mounts so they survive recreation).
    ROLLING_RECREATE = "rolling_recreate"

    # postgresql / redis: only swap the image tag and restart in order;
    # never remove the persistent volume without explicit user action.
    TAG_SWAP_RESTART = "tag_swap_restart"


@dataclass(slots=True, frozen=True)
class MCStackVersionInfo:
    """Snapshot of the desired versions for the four stack components."""

    mc_bd: str | None
    mc_fd: str | None
    postgresql: str | None
    redis: str | None

    @property
    def is_complete(self) -> bool:
        """Return True if every component has a configured version."""
        return all((self.mc_bd, self.mc_fd, self.postgresql, self.redis))


@dataclass(slots=True, frozen=True)
class MCStackComponentHealth:
    """Health snapshot of a single MC stack component."""

    name: str
    state: ContainerState
    healthy: bool

    @property
    def degraded(self) -> bool:
        """Return True if the component is failed / stopped / unhealthy."""
        return (
            self.state
            in (
                ContainerState.FAILED,
                ContainerState.STOPPED,
                ContainerState.UNHEALTHY,
                ContainerState.UNKNOWN,
            )
            or not self.healthy
        )


class MCStack(CoreSysAttributes):
    """Supervisor-managed MC application stack."""

    def __init__(self, coresys: CoreSys) -> None:
        """Initialize stack facades and helpers."""
        self.coresys = coresys
        self._secrets = MCStackSecrets(coresys)
        self._config = MCStackConfig(coresys)
        self.postgres = DockerMcPostgres(coresys)
        self.redis = DockerMcRedis(coresys)
        self.backend = DockerMcBackend(coresys)
        self.frontend = DockerMcFrontend(coresys)

    # --- Public properties -------------------------------------------------

    @property
    def secrets(self) -> MCStackSecrets:
        """Return secrets store (PostgreSQL / Redis credentials)."""
        return self._secrets

    @property
    def config(self) -> MCStackConfig:
        """Return runtime config store (boot / watchdog flags)."""
        return self._config

    @property
    def boot(self) -> bool:
        """Return True if the operator wants the stack to auto-start."""
        return self._config.boot

    @boot.setter
    def boot(self, value: bool) -> None:
        """Persist the auto-start preference."""
        self._config.boot = value

    @property
    def watchdog(self) -> bool:
        """Return True if the periodic watchdog should act on the stack."""
        return self._config.watchdog

    @watchdog.setter
    def watchdog(self, value: bool) -> None:
        """Persist the watchdog enable flag."""
        self._config.watchdog = value

    @property
    def components(self) -> tuple[DockerInterface, ...]:
        """Return components in dependency order (postgres first)."""
        return (self.postgres, self.redis, self.backend, self.frontend)

    @property
    def version_info(self) -> MCStackVersionInfo:
        """Return desired versions for all components."""
        updater = self.sys_updater
        return MCStackVersionInfo(
            mc_bd=str(updater.version_mc_bd) if updater.version_mc_bd else None,
            mc_fd=str(updater.version_mc_fd) if updater.version_mc_fd else None,
            postgresql=(
                str(updater.version_postgresql) if updater.version_postgresql else None
            ),
            redis=str(updater.version_redis) if updater.version_redis else None,
        )

    @property
    def enabled(self) -> bool:
        """Return True if the MC stack should be managed.

        We treat a missing image template (``image_*`` returning ``None``)
        as "feature disabled" so legacy ``version`` JSONs without the new
        keys keep working until the operator opts in.
        """
        return self.version_info.is_complete and all(
            inst.image for inst in self.components
        )

    # --- Lifecycle ---------------------------------------------------------

    async def load(self) -> None:
        """Load persisted stack state and attach to running containers.

        Mirrors what the existing plug-ins do during Supervisor startup so a
        restart of just the Supervisor process keeps existing containers in
        place.
        """
        await self._config.load_config()
        await self._secrets.load_config()
        await self._secrets.ensure()

        if not self.enabled:
            _LOGGER.debug("MC stack disabled (no version data); skipping attach")
            return

        for inst in self.components:
            version = inst.version
            if not version:
                continue
            try:
                await inst.attach(version=version, skip_state_event_if_down=True)
            except DockerError:
                _LOGGER.info(
                    "MC stack: %s container not yet present, will create on start",
                    inst.name,
                )

        await self.sync_dns()

    async def inspect_container(
        self, inst: DockerInterface
    ) -> dict[str, Any] | None:
        """Return Docker inspect metadata for a stack container."""
        try:
            container = await self.sys_docker.containers.get(inst.name)
            return await container.show()
        except aiodocker.DockerError as err:
            if err.status == HTTPStatus.NOT_FOUND:
                return None
            raise DockerError(
                f"Failed to inspect MC stack container {inst.name}: {err!s}",
                _LOGGER.error,
            ) from err

    async def sync_dns(self) -> None:
        """Publish MC stack aliases into CoreDNS ``hosts`` (like add-ons).

        Docker embedded DNS (127.0.0.11) does not always resolve stack
        aliases in MCOS/QEMU even when ``resolv.conf`` lists it first.
        Registering the IPs with plugin-dns makes ``mc_redis`` /
        ``mc_postgres`` reachable via the CoreDNS nameserver every
        container already uses.
        """
        registry: tuple[tuple[DockerInterface, tuple[str, ...]], ...] = (
            (self.postgres, MC_POSTGRES_DNS_ALIASES),
            (self.redis, MC_REDIS_DNS_ALIASES),
            (self.backend, MC_BACKEND_DNS_ALIASES),
            (self.frontend, MC_FRONTEND_DNS_ALIASES),
        )
        add_host_coros: list[Awaitable[None]] = []
        for inst, aliases in registry:
            if not await inst.is_running():
                continue
            metadata = await self.inspect_container(inst)
            ip = mc_stack_container_ip(metadata)
            if not ip:
                continue
            add_host_coros.append(
                self.sys_plugins.dns.add_host(ipv4=ip, names=list(aliases), write=False)
            )

        if not add_host_coros:
            return

        await asyncio.gather(*add_host_coros)
        with suppress(CoreDNSError):
            await self.sys_plugins.dns.write_hosts()

    async def dependency_extra_hosts(
        self,
        *dependencies: tuple[DockerInterface, tuple[str, ...]],
    ) -> dict[str, IPv4Address]:
        """Resolve MC stack aliases to IPs for ``ExtraHosts`` injection.

        Docker embedded DNS does not reliably resolve ``mcos`` network
        aliases in MCOS/QEMU. Writing names into ``/etc/hosts`` at
        container create time avoids Redis/Postgres connection timeouts
        in ``mc_bd`` even when CoreDNS or ``127.0.0.11`` fail.
        """
        entries: list[tuple[dict[str, Any] | None, tuple[str, ...]]] = []
        for inst, aliases in dependencies:
            entries.append((await self.inspect_container(inst), aliases))
        return mc_stack_alias_hosts(*entries)

    async def start(self) -> None:
        """Start the four core containers in dependency order.

        Each component is installed on demand if its image is missing, then
        started, and finally awaited for readiness. The whole flow is
        wrapped in a generous total timeout to avoid a single hang stalling
        Supervisor startup.
        """
        if not self.enabled:
            _LOGGER.info("MC stack: not all four images are configured; staying idle")
            return

        try:
            async with asyncio.timeout(_TOTAL_START_TIMEOUT.total_seconds()):
                await self._start_component(
                    self.postgres,
                    timeout=_TIMEOUT_POSTGRES,
                    health=self._check_postgres_ready,
                )
                await self._ensure_postgres_database()
                await self._start_component(
                    self.redis,
                    timeout=_TIMEOUT_REDIS,
                    health=self._check_redis_ready,
                )
                await self._start_component(
                    self.backend,
                    timeout=_TIMEOUT_MC_BD,
                    health=self._check_backend_ready,
                )
                await self._start_component(
                    self.frontend,
                    timeout=_TIMEOUT_MC_FD,
                    health=self._check_frontend_ready,
                )
        except TimeoutError as err:
            raise MCStackStartupError(
                "MC stack failed to come up within the total budget",
                _LOGGER.error,
            ) from err

    async def stop(self, *, remove_container: bool = False) -> None:
        """Stop containers in reverse dependency order.

        Docker volumes are kept (PostgreSQL data must never be removed
        without operator confirmation per the A1 plan).
        """
        for inst in reversed(self.components):
            with suppress(DockerError):
                await inst.stop(remove_container=remove_container)

    async def restart(self) -> None:
        """Restart the whole stack (stop → start)."""
        await self.stop()
        await self.start()

    async def healthcheck(self) -> dict[str, MCStackComponentHealth]:
        """Return a structured health snapshot used by Resolution / API.

        Each entry combines ``ContainerState`` (from Docker) with the
        application-level readiness probe used at startup, giving us a
        single signal that "the component is up *and* answering". This
        is what Stage 5 watchdogs and Stage 6 resolution evaluations
        consume.
        """
        if not self.enabled:
            return {}

        probes: list[tuple[DockerInterface, Callable[[], Awaitable[bool]]]] = [
            (self.postgres, self._check_postgres_ready),
            (self.redis, self._check_redis_ready),
            (self.backend, self._check_backend_ready),
            (self.frontend, self._check_frontend_ready),
        ]

        result: dict[str, MCStackComponentHealth] = {}
        for inst, probe in probes:
            state = await inst.current_state()
            healthy = (
                await probe()
                if state in (ContainerState.RUNNING, ContainerState.HEALTHY)
                else False
            )
            result[inst.name] = MCStackComponentHealth(
                name=inst.name, state=state, healthy=healthy
            )
        return result

    async def update(self) -> None:
        """Pull and re-deploy any component whose desired version changed.

        Per the A1 plan stage 4 升级策略:

        * ``mc_bd`` / ``mc_fd``: rolling recreate — pull image, stop old
          container, restart with the new tag (volumes preserved).
        * ``postgresql`` / ``redis``: only swap the image tag and perform
          an ordered restart. The persistent data volumes (bind mounts)
          are *never* removed by this method, even on tag changes.
        """
        if not self.enabled:
            return

        info = self.version_info
        plan: list[tuple[DockerInterface, str | None, MCStackUpdateStrategy]] = [
            (self.postgres, info.postgresql, MCStackUpdateStrategy.TAG_SWAP_RESTART),
            (self.redis, info.redis, MCStackUpdateStrategy.TAG_SWAP_RESTART),
            (self.backend, info.mc_bd, MCStackUpdateStrategy.ROLLING_RECREATE),
            (self.frontend, info.mc_fd, MCStackUpdateStrategy.ROLLING_RECREATE),
        ]

        any_updated = False
        for inst, target, strategy in plan:
            if not target:
                continue
            if not self._needs_update(inst.version, target):
                continue
            _LOGGER.info(
                "MC stack: updating %s %s → %s using %s strategy",
                inst.name,
                inst.version,
                target,
                strategy.value,
            )
            try:
                # ``DockerInterface.update`` pulls the new image then stops
                # the old container. The persistent bind mount is untouched
                # in both strategies; the difference is purely semantic /
                # documentation for operators (and may diverge later if we
                # add e.g. pre-update ``pg_dump`` for ``TAG_SWAP_RESTART``).
                await inst.update(AwesomeVersion(target), image=inst.image)
            except DockerError as err:
                raise MCStackUpdateError(
                    f"Failed to update MC stack component {inst.name}: {err!s}",
                    _LOGGER.error,
                ) from err
            any_updated = True

        if not any_updated:
            _LOGGER.debug("MC stack: nothing to update")
            return

        # Restart in dependency order so dependents see refreshed services.
        # We do *not* delete data volumes — even on a tag swap, PostgreSQL /
        # Redis must be able to read their on-disk format from the existing
        # bind mount.
        await self.restart()

    @staticmethod
    def _needs_update(current: AwesomeVersion | None, target: str) -> bool:
        """Return True iff target is newer than (or different from) current.

        Falls back to a string comparison if ``AwesomeVersion`` cannot
        compare the two (e.g. mixed CalVer / SemVer); in that case any
        textual difference counts as "needs update".
        """
        if current is None:
            return True
        try:
            return AwesomeVersion(target) != current
        except AwesomeVersionException:
            return str(current) != target

    # --- Internal helpers --------------------------------------------------

    async def _start_component(
        self,
        inst: DockerInterface,
        *,
        timeout: timedelta,
        health: Callable[[], Awaitable[bool]],
    ) -> None:
        """Install (if needed), run and wait for readiness of a component."""
        version = inst.version
        if not version:
            raise MCStackError(
                f"MC stack: no version configured for {inst.name}",
                _LOGGER.error,
            )

        if await inst.is_running():
            if inst in (self.backend, self.frontend):
                _LOGGER.info(
                    "MC stack: recreating %s to refresh dependency networking",
                    inst.name,
                )
                await inst.stop(remove_container=True)
            else:
                _LOGGER.debug("MC stack: %s already running", inst.name)

        if not await inst.is_running():
            if not await inst.exists():
                _LOGGER.info(
                    "MC stack: pulling %s:%s for %s",
                    inst.image,
                    version,
                    inst.name,
                )
                try:
                    await inst.install(version, image=inst.image)
                except DockerError as err:
                    raise MCStackStartupError(
                        f"Pulling {inst.image}:{version} failed: {err!s}",
                        _LOGGER.error,
                    ) from err

            try:
                await inst.run()
            except DockerError as err:
                raise MCStackStartupError(
                    f"Running container {inst.name} failed: {err!s}",
                    _LOGGER.error,
                ) from err

        try:
            async with asyncio.timeout(timeout.total_seconds()):
                while True:
                    if await health():
                        _LOGGER.info("MC stack: %s reported healthy", inst.name)
                        await self.sync_dns()
                        return
                    if await inst.current_state() in (
                        ContainerState.FAILED,
                        ContainerState.STOPPED,
                    ):
                        raise MCStackStartupError(
                            f"Container {inst.name} died before becoming healthy",
                            _LOGGER.error,
                        )
                    await asyncio.sleep(_HEALTH_POLL_SECONDS)
        except TimeoutError as err:
            raise MCStackStartupError(
                f"Timeout waiting for {inst.name} to become healthy",
                _LOGGER.error,
            ) from err

    async def _check_postgres_ready(self) -> bool:
        """Return True if PostgreSQL accepts connections (pg_isready in-container)."""
        try:
            result = await self.postgres.run_inside(
                f"pg_isready -U {MC_POSTGRES_DEFAULT_USER}"
            )
        except DockerError:
            return False
        return result.exit_code == 0

    async def _ensure_postgres_database(self) -> None:
        """Create the mc_bd application database when missing.

        Fresh volumes get ``POSTGRES_DB=mc`` at init time. Legacy volumes
        created with ``POSTGRES_DB=postgres`` only have the default database;
        create ``mc`` before starting mc_bd so ``create_tables()`` can connect.
        """
        db = MC_POSTGRES_DEFAULT_DB
        user = MC_POSTGRES_DEFAULT_USER
        if db != "mc" or user != "postgres":
            raise MCStackStartupError(
                f"Unsupported MC stack PostgreSQL defaults: db={db!r} user={user!r}",
                _LOGGER.error,
            )

        try:
            check = await self.postgres.run_inside(
                'psql -U postgres -tc '
                '"SELECT 1 FROM pg_database WHERE datname = \'mc\'"'
            )
        except DockerError as err:
            raise MCStackStartupError(
                f"Failed to inspect PostgreSQL databases: {err!s}",
                _LOGGER.error,
            ) from err

        if check.exit_code == 0 and b"1" in check.output:
            return

        _LOGGER.info("MC stack: creating PostgreSQL database %s", db)
        try:
            create = await self.postgres.run_inside(
                'psql -U postgres -c "CREATE DATABASE mc;"'
            )
        except DockerError as err:
            raise MCStackStartupError(
                f"Failed to create PostgreSQL database {db}: {err!s}",
                _LOGGER.error,
            ) from err

        if create.exit_code != 0:
            raise MCStackStartupError(
                f"CREATE DATABASE {db} failed: {create.output!r}",
                _LOGGER.error,
            )

    async def _check_redis_ready(self) -> bool:
        """Return True if Redis answers PING."""
        try:
            result = await self.redis.run_inside("redis-cli ping")
        except DockerError:
            return False
        return result.exit_code == 0 and b"PONG" in result.output

    async def _check_backend_ready(self) -> bool:
        """HTTP-poll mc_bd's health endpoint."""
        return await self._http_alive(
            host="mc_bd",
            port=MC_BACKEND_PORT,
            path=MC_BACKEND_HEALTH_PATH,
        )

    async def _check_frontend_ready(self) -> bool:
        """Treat ``mc_fd`` as ready when its Nginx port answers."""
        return await self._http_alive(
            host="mc_fd",
            port=MC_FRONTEND_PORT,
            path="/",
            accept_status_below=500,
        )

    async def _http_alive(
        self,
        *,
        host: str,
        port: int,
        path: str,
        accept_status_below: int = 500,
    ) -> bool:
        """Return True if a 2xx/3xx/4xx response comes back from host:port."""
        url = f"http://{host}:{port}{path}"
        try:
            timeout = aiohttp.ClientTimeout(total=2)
            async with self.sys_websession.get(url, timeout=timeout) as response:
                return response.status < accept_status_below
        except (aiohttp.ClientError, TimeoutError):
            return False
