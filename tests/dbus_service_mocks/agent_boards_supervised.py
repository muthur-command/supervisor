"""Mock of OS Agent Boards Supervised dbus service."""

from .base import DBusServiceMock

BUS_NAME = "io.muthurcommand.os"


def setup(object_path: str | None = None) -> DBusServiceMock:
    """Create dbus mock object."""
    return Supervised()


class Supervised(DBusServiceMock):
    """Supervised mock.

    gdbus introspect --system --dest io.muthurcommand.os --object-path /io/muthurcommand/os/Boards/Supervised
    """

    object_path = "/io/muthurcommand/os/Boards/Supervised"
    interface = "io.muthurcommand.os.Boards.Supervised"
