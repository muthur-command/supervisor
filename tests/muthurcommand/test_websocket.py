"""Test websocket."""

# pylint: disable=import-error
import asyncio
from unittest.mock import AsyncMock, patch

from awesomeversion import AwesomeVersion
import pytest

from supervisor.const import CoreState
from supervisor.coresys import CoreSys
from supervisor.exceptions import MuthurCommandWSConnectionError
from supervisor.muthurcommand.const import WSEvent, WSType


@pytest.fixture
def ha_core_configured(coresys: CoreSys) -> None:
    """Pretend Home Assistant Core is installed (defeats the ``unused`` short-circuit).

    The default ``coresys`` fixture leaves both ``version`` and
    ``latest_version`` unset, which the websocket now treats as "MCOS
    image with no Home Assistant Core" (Stage 5). Tests that exercise
    the legacy "Core is configured but unreachable" branch need an
    explicit version.
    """
    coresys.muthurcommand.version = AwesomeVersion("2024.1.0")
    coresys.updater._data["muthurcommand"] = AwesomeVersion("2024.1.0")  # noqa: SLF001


async def test_send_command(coresys: CoreSys, ha_ws_client: AsyncMock):
    """Test sending a command returns a response."""
    await coresys.muthurcommand.websocket.async_send_command({"type": "test"})
    ha_ws_client.async_send_command.assert_called_with({"type": "test"})

    await coresys.muthurcommand.websocket.async_supervisor_update_event(
        "test", {"lorem": "ipsum"}
    )
    ha_ws_client.async_send_command.assert_called_with(
        {
            "type": WSType.SUPERVISOR_EVENT,
            "data": {
                "event": WSEvent.SUPERVISOR_UPDATE,
                "update_key": "test",
                "data": {"lorem": "ipsum"},
            },
        }
    )


async def test_fire_and_forget_during_startup(
    coresys: CoreSys, ha_ws_client: AsyncMock
):
    """Test fire-and-forget commands queue during startup and replay when running."""
    await coresys.muthurcommand.websocket.load()
    await coresys.core.set_state(CoreState.SETUP)

    await coresys.muthurcommand.websocket.async_supervisor_update_event(
        "test", {"lorem": "ipsum"}
    )
    ha_ws_client.async_send_command.assert_not_called()

    await coresys.core.set_state(CoreState.RUNNING)
    await asyncio.sleep(0)

    assert ha_ws_client.async_send_command.call_count == 2
    assert ha_ws_client.async_send_command.call_args_list[0][0][0] == {
        "type": WSType.SUPERVISOR_EVENT,
        "data": {
            "event": WSEvent.SUPERVISOR_UPDATE,
            "update_key": "test",
            "data": {"lorem": "ipsum"},
        },
    }
    assert ha_ws_client.async_send_command.call_args_list[1][0][0] == {
        "type": WSType.SUPERVISOR_EVENT,
        "data": {
            "event": WSEvent.SUPERVISOR_UPDATE,
            "update_key": "info",
            "data": {"state": "running"},
        },
    }

    ha_ws_client.reset_mock()
    await coresys.core.set_state(CoreState.SHUTDOWN)

    await coresys.muthurcommand.websocket.async_supervisor_update_event(
        "test", {"lorem": "ipsum"}
    )
    ha_ws_client.async_send_command.assert_not_called()


async def test_send_command_core_not_reachable(
    coresys: CoreSys, ha_ws_client: AsyncMock, ha_core_configured: None
):
    """Test async_send_command raises when Core API is not reachable."""
    ha_ws_client.connected = False
    with (
        patch.object(coresys.muthurcommand.api, "check_api_state", return_value=False),
        pytest.raises(MuthurCommandWSConnectionError, match="not reachable"),
    ):
        await coresys.muthurcommand.websocket.async_send_command({"type": "test"})

    ha_ws_client.async_send_command.assert_not_called()


async def test_fire_and_forget_core_not_reachable(
    coresys: CoreSys, ha_ws_client: AsyncMock, ha_core_configured: None
):
    """Test fire-and-forget command silently skips when Core API is not reachable."""
    ha_ws_client.connected = False
    with patch.object(coresys.muthurcommand.api, "check_api_state", return_value=False):
        await coresys.muthurcommand.websocket._async_send_command({"type": "test"})

    ha_ws_client.async_send_command.assert_not_called()


async def test_send_command_blocked_when_ha_core_unused(
    coresys: CoreSys, ha_ws_client: AsyncMock
):
    """A new WebSocket connection is refused on MCOS images without HA Core."""
    # Default conftest leaves ``version``/``latest_version`` unset → unused.
    assert coresys.muthurcommand.unused is True
    ha_ws_client.connected = False
    with pytest.raises(MuthurCommandWSConnectionError, match="unused"):
        await coresys.muthurcommand.websocket.async_send_command({"type": "test"})
    ha_ws_client.async_send_command.assert_not_called()


async def test_send_command_during_shutdown(coresys: CoreSys, ha_ws_client: AsyncMock):
    """Test async_send_command raises during shutdown."""
    await coresys.core.set_state(CoreState.SHUTDOWN)
    with pytest.raises(MuthurCommandWSConnectionError, match="shutting down"):
        await coresys.muthurcommand.websocket.async_send_command({"type": "test"})

    ha_ws_client.async_send_command.assert_not_called()
