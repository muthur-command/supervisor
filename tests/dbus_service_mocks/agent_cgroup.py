"""Mock of OS Agent CGroup dbus service."""

from .base import DBusServiceMock, dbus_method

BUS_NAME = "io.muthurcommand.os"


def setup(object_path: str | None = None) -> DBusServiceMock:
    """Create dbus mock object."""
    return CGroup()


class CGroup(DBusServiceMock):
    """CGroup mock.

    gdbus introspect --system --dest io.muthurcommand.os --object-path /io/muthurcommand/os/CGroup
    """

    object_path = "/io/muthurcommand/os/CGroup"
    interface = "io.muthurcommand.os.CGroup"

    @dbus_method()
    def AddDevicesAllowed(self, arg_0: "s", arg_1: "s") -> "b":
        """Load profile."""
        return True
