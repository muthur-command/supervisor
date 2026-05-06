"""Mock of OS Agent System dbus service."""

from dbus_fast import DBusError, ErrorType

from .base import DBusServiceMock, dbus_method

BUS_NAME = "io.muthurcommand.os"


def setup(object_path: str | None = None) -> DBusServiceMock:
    """Create dbus mock object."""
    return System()


class System(DBusServiceMock):
    """System mock.

    gdbus introspect --system --dest io.muthurcommand.os --object-path /io/muthurcommand/os/System
    """

    object_path = "/io/muthurcommand/os/System"
    interface = "io.muthurcommand.os.System"
    response_schedule_wipe_device: bool | DBusError = True
    response_migrate_docker_storage_driver: None | DBusError = None

    @dbus_method()
    def ScheduleWipeDevice(self) -> "b":
        """Schedule wipe device."""
        if isinstance(self.response_schedule_wipe_device, DBusError):
            raise self.response_schedule_wipe_device  # pylint: disable=raising-bad-type
        return self.response_schedule_wipe_device

    @dbus_method()
    def MigrateDockerStorageDriver(self, backend: "s") -> None:
        """Migrate Docker storage driver."""
        if isinstance(self.response_migrate_docker_storage_driver, DBusError):
            raise self.response_migrate_docker_storage_driver  # pylint: disable=raising-bad-type
        if backend != "overlayfs":
            raise DBusError(
                ErrorType.FAILED,
                f"unsupported driver: {backend} (only 'overlayfs' is currently supported)",
            )
