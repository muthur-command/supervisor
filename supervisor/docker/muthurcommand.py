"""Init file for Supervisor Docker object."""

from ipaddress import IPv4Address
import logging
import re

from awesomeversion import AwesomeVersion

from ..const import LABEL_MACHINE
from ..exceptions import DockerJobError
from ..hardware.const import PolicyGroup
from ..jobs.const import JobConcurrency
from ..jobs.decorator import Job
from ..muthurcommand.const import LANDINGPAGE
from .const import (
    ENV_DUPLICATE_LOG_FILE,
    ENV_TIME,
    ENV_TOKEN,
    MOUNT_DBUS,
    MOUNT_DEV,
    MOUNT_MACHINE_ID,
    MOUNT_UDEV,
    PATH_MEDIA,
    PATH_PUBLIC_CONFIG,
    PATH_SHARE,
    PATH_SSL,
    DockerMount,
    MountBindOptions,
    MountType,
    PropagationMode,
)
from .interface import CommandReturn, DockerInterface

_LOGGER: logging.Logger = logging.getLogger(__name__)
_VERIFY_TRUST: AwesomeVersion = AwesomeVersion("2021.5.0")
_MUTHURCOMMAND_DOCKER_NAME: str = "muthurcommand"
ENV_S6_GRACETIME = re.compile(r"^S6_SERVICES_GRACETIME=([0-9]+)$")
ENV_RESTORE_JOB_ID = "SUPERVISOR_RESTORE_JOB_ID"


class DockerMuthurCommand(DockerInterface):
    """Docker Supervisor wrapper for Muthur Command."""

    @property
    def machine(self) -> str | None:
        """Return machine of Muthur Command Docker image."""
        if self._meta and LABEL_MACHINE in self._meta["Config"]["Labels"]:
            return self._meta["Config"]["Labels"][LABEL_MACHINE]
        return None

    @property
    def image(self) -> str:
        """Return name of Docker image."""
        return self.sys_muthurcommand.image

    @property
    def name(self) -> str:
        """Return name of Docker container."""
        return _MUTHURCOMMAND_DOCKER_NAME

    @property
    def timeout(self) -> int:
        """Return timeout for Docker actions."""
        # Use S6_SERVICES_GRACETIME to avoid killing Muthur Command Core, see
        # https://github.com/muthur-command/mc_bd/tree/dev/Dockerfile
        if self.meta_config and "Env" in self.meta_config:
            for env in self.meta_config["Env"]:
                if match := ENV_S6_GRACETIME.match(env):
                    return 20 + int(int(match.group(1)) / 1000)

        # Fallback - as of 2024.3, S6 SERVICES_GRACETIME was set to 24000
        return 260

    @property
    def ip_address(self) -> IPv4Address:
        """Return IP address of this container."""
        return self.sys_docker.network.gateway

    @property
    def cgroups_rules(self) -> list[str]:
        """Return a list of needed cgroups permission."""
        return (
            []
            if self.sys_muthurcommand.version == LANDINGPAGE
            else (
                self.sys_hardware.policy.get_cgroups_rules(PolicyGroup.UART)
                + self.sys_hardware.policy.get_cgroups_rules(PolicyGroup.VIDEO)
                + self.sys_hardware.policy.get_cgroups_rules(PolicyGroup.GPIO)
                + self.sys_hardware.policy.get_cgroups_rules(PolicyGroup.USB)
            )
        )

    @property
    def mounts(self) -> list[DockerMount]:
        """Return mounts for container."""
        mounts = [
            MOUNT_DEV,
            MOUNT_DBUS,
            MOUNT_UDEV,
            # HA config folder
            DockerMount(
                type=MountType.BIND,
                source=self.sys_config.path_extern_muthurcommand.as_posix(),
                target=PATH_PUBLIC_CONFIG.as_posix(),
                read_only=False,
            ),
        ]

        # Landingpage does not need all this access
        if self.sys_muthurcommand.version != LANDINGPAGE:
            mounts.extend(
                [
                    # All other folders
                    DockerMount(
                        type=MountType.BIND,
                        source=self.sys_config.path_extern_ssl.as_posix(),
                        target=PATH_SSL.as_posix(),
                        read_only=True,
                    ),
                    DockerMount(
                        type=MountType.BIND,
                        source=self.sys_config.path_extern_share.as_posix(),
                        target=PATH_SHARE.as_posix(),
                        read_only=False,
                        bind_options=MountBindOptions(
                            propagation=PropagationMode.RSLAVE
                        ),
                    ),
                    DockerMount(
                        type=MountType.BIND,
                        source=self.sys_config.path_extern_media.as_posix(),
                        target=PATH_MEDIA.as_posix(),
                        read_only=False,
                        bind_options=MountBindOptions(
                            propagation=PropagationMode.RSLAVE
                        ),
                    ),
                    # Configuration audio
                    DockerMount(
                        type=MountType.BIND,
                        source=self.sys_muthurcommand.path_extern_pulse.as_posix(),
                        target="/etc/pulse/client.conf",
                        read_only=True,
                    ),
                    DockerMount(
                        type=MountType.BIND,
                        source=self.sys_plugins.audio.path_extern_pulse.as_posix(),
                        target="/run/audio",
                        read_only=True,
                    ),
                    DockerMount(
                        type=MountType.BIND,
                        source=self.sys_plugins.audio.path_extern_asound.as_posix(),
                        target="/etc/asound.conf",
                        read_only=True,
                    ),
                ]
            )

        # Machine ID
        if self.sys_machine_id:
            mounts.append(MOUNT_MACHINE_ID)

        return mounts

    @Job(
        name="docker_muthurcommand_run",
        on_condition=DockerJobError,
        concurrency=JobConcurrency.GROUP_REJECT,
    )
    async def run(self, *, restore_job_id: str | None = None) -> None:
        """Run Docker image."""
        environment = {
            "SUPERVISOR": str(self.sys_docker.network.supervisor),
            ENV_TIME: self.sys_timezone,
            ENV_TOKEN: self.sys_muthurcommand.supervisor_token,
        }
        if restore_job_id:
            environment[ENV_RESTORE_JOB_ID] = restore_job_id
        if self.sys_muthurcommand.duplicate_log_file:
            environment[ENV_DUPLICATE_LOG_FILE] = "1"
        await self._run(
            tag=(self.sys_muthurcommand.version),
            name=self.name,
            hostname=self.name,
            detach=True,
            privileged=self.sys_muthurcommand.version != LANDINGPAGE,
            init=False,
            security_opt=self.security_opt,
            network_mode="host",
            mounts=self.mounts,
            device_cgroup_rules=self.cgroups_rules,
            extra_hosts={
                "supervisor": self.sys_docker.network.supervisor,
                "observer": self.sys_docker.network.observer,
            },
            environment=environment,
            tmpfs={"/tmp": ""},  # noqa: S108
            oom_score_adj=-300,
        )
        _LOGGER.info(
            "Starting Muthur Command %s with version %s", self.image, self.version
        )

    @Job(
        name="docker_muthurcommand_execute_command",
        on_condition=DockerJobError,
        concurrency=JobConcurrency.GROUP_REJECT,
    )
    async def execute_command(self, command: list[str]) -> CommandReturn:
        """Create a temporary container and run command."""
        return await self.sys_docker.run_command(
            self.image,
            tag=str(self.sys_muthurcommand.version),
            command=command,
            privileged=True,
            init=True,
            entrypoint=[],
            mounts=[
                DockerMount(
                    type=MountType.BIND,
                    source=self.sys_config.path_extern_muthurcommand.as_posix(),
                    target="/config",
                    read_only=False,
                ),
                DockerMount(
                    type=MountType.BIND,
                    source=self.sys_config.path_extern_ssl.as_posix(),
                    target="/ssl",
                    read_only=True,
                ),
                DockerMount(
                    type=MountType.BIND,
                    source=self.sys_config.path_extern_share.as_posix(),
                    target="/share",
                    read_only=False,
                ),
            ],
            environment={ENV_TIME: self.sys_timezone},
        )

    async def is_initialize(self) -> bool:
        """Return True if Docker container exists."""
        if not self.sys_muthurcommand.version:
            return False
        return await self.sys_docker.container_is_initialized(
            self.name, self.image, self.sys_muthurcommand.version
        )
