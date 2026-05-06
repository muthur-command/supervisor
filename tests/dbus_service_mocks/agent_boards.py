"""Mock of OS Agent Boards dbus service."""

from dbus_fast.service import PropertyAccess, dbus_property

from .base import DBusServiceMock

BUS_NAME = "io.muthurcommand.os"


def setup(object_path: str | None = None) -> DBusServiceMock:
    """Create dbus mock object."""
    return Boards()


class Boards(DBusServiceMock):
    """Boards mock.

    gdbus introspect --system --dest io.muthurcommand.os --object-path /io/muthurcommand/os/Boards
    """

    object_path = "/io/muthurcommand/os/Boards"
    interface = "io.muthurcommand.os.Boards"
    board = "Yellow"

    @dbus_property(access=PropertyAccess.READ)
    def Board(self) -> "s":
        """Get Board."""
        return self.board
