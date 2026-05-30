"""Test Muthurcommand module."""

import asyncio
import errno
import logging
from pathlib import Path, PurePath
from unittest.mock import AsyncMock, patch

import pytest

from supervisor.backups.backup import Backup
from supervisor.backups.const import BackupType
from supervisor.const import CoreState
from supervisor.coresys import CoreSys
from supervisor.docker.interface import DockerInterface
from supervisor.exceptions import (
    MuthurCommandBackupError,
    MuthurCommandWSConnectionError,
)
from supervisor.muthurcommand.module import MuthurCommand
from supervisor.muthurcommand.secrets import MuthurCommandSecrets
from supervisor.muthurcommand.websocket import MuthurCommandWebSocket
from supervisor.utils.dt import utcnow


async def test_load(
    coresys: CoreSys, tmp_supervisor_data: Path, ha_ws_client: AsyncMock
):
    """Test muthurcommand module load."""
    with open(
        tmp_supervisor_data / "muthurcommand" / "secrets.yaml", "w", encoding="utf-8"
    ) as secrets:
        secrets.write("hello: world\n")

    # Unwrap read_secrets to prevent throttling between tests
    with (
        patch.object(DockerInterface, "attach") as attach,
        patch.object(DockerInterface, "check_image") as check_image,
        patch.object(
            MuthurCommandSecrets,
            "_read_secrets",
            new=MuthurCommandSecrets._read_secrets.__wrapped__,  # pylint: disable=protected-access,no-member
        ),
    ):
        await coresys.muthurcommand.load()

        attach.assert_called_once()
        check_image.assert_called_once()

    assert coresys.muthurcommand.secrets.secrets == {"hello": "world"}

    await coresys.core.set_state(CoreState.SETUP)
    await coresys.muthurcommand.websocket._async_send_command({"lorem": "ipsum"})
    ha_ws_client.async_send_command.assert_not_called()

    await coresys.core.set_state(CoreState.RUNNING)
    await asyncio.sleep(0)
    assert ha_ws_client.async_send_command.call_args_list[0][0][0] == {"lorem": "ipsum"}


async def test_list_users_none(coresys: CoreSys, ha_ws_client: AsyncMock):
    """Test list users raises on unexpected None response from Core."""
    ha_ws_client.async_send_command.return_value = None
    with pytest.raises(TypeError):
        await coresys.muthurcommand.list_users()


async def test_write_pulse_error(coresys: CoreSys, caplog: pytest.LogCaptureFixture):
    """Test errors writing pulse config."""
    with patch(
        "supervisor.muthurcommand.module.Path.write_text",
        side_effect=(err := OSError()),
    ):
        err.errno = errno.EBUSY
        await coresys.muthurcommand.write_pulse()

        assert "can't write pulse/client.config" in caplog.text
        assert coresys.core.healthy is True

        caplog.clear()
        err.errno = errno.EBADMSG
        await coresys.muthurcommand.write_pulse()

        assert "can't write pulse/client.config" in caplog.text
        assert coresys.core.healthy is False


async def test_begin_backup_ws_error(coresys: CoreSys):
    """Test WS error when beginning backup."""
    # pylint: disable-next=protected-access
    coresys.muthurcommand.websocket._client.async_send_command.side_effect = (
        MuthurCommandWSConnectionError("Connection was closed")
    )
    with (
        patch.object(MuthurCommandWebSocket, "_ensure_connected", return_value=None),
        pytest.raises(
            MuthurCommandBackupError,
            match="Preparing backup of Muthur Command Core failed. Failed to inform Muthur Command Core: Connection was closed.",
        ),
    ):
        await coresys.muthurcommand.begin_backup()


async def test_end_backup_ws_error(coresys: CoreSys, caplog: pytest.LogCaptureFixture):
    """Test WS error when ending backup."""
    # pylint: disable-next=protected-access
    coresys.muthurcommand.websocket._client.async_send_command.side_effect = (
        MuthurCommandWSConnectionError("Connection was closed")
    )
    with patch.object(MuthurCommandWebSocket, "_ensure_connected", return_value=None):
        await coresys.muthurcommand.end_backup()

    assert (
        "Error resuming normal operations after backup of Muthur Command Core. Failed to inform Muthur Command Core: Connection was closed."
        in caplog.text
    )


@pytest.mark.parametrize(
    ("filename", "exclude_db", "expect_excluded", "subfolder"),
    [
        ("muthurcommand.log", False, True, None),
        ("muthurcommand.log.1", False, True, None),
        ("muthurcommand.log.fault", False, True, None),
        ("muthurcommand.log", False, False, "subfolder"),
        ("OZW_Log.txt", False, True, None),
        ("OZW_Log.txt", False, False, "subfolder"),
        ("muthurcommand_v2.db-shm", False, True, None),
        ("muthurcommand_v2.db-shm", False, False, "subfolder"),
        ("muthurcommand_v2.db", False, False, None),
        ("muthurcommand_v2.db", True, True, None),
        ("muthurcommand_v2.db", True, False, "subfolder"),
        ("muthurcommand_v2.db-wal", False, False, None),
        ("muthurcommand_v2.db-wal", True, True, None),
        ("muthurcommand_v2.db-wal", True, False, "subfolder"),
        ("test.tar", False, True, "backups"),
        ("test.tar", False, False, "subfolder/backups"),
        ("test.tar", False, True, "tmp_backups"),
        ("test.tar", False, False, "subfolder/tmp_backups"),
        ("test", False, True, "tts"),
        ("test", False, False, "subfolder/tts"),
        ("test.cpython-312.pyc", False, True, "__pycache__"),
        ("test.cpython-312.pyc", False, True, "subfolder/__pycache__"),
        (".DS_Store", False, True, None),
        (".DS_Store", False, True, "subfolder"),
        (
            "core.restore_state.corrupt.2025-03-26T20:55:45.635297+00:00",
            False,
            True,
            ".storage",
        ),
    ],
)
@pytest.mark.usefixtures("tmp_supervisor_data")
async def test_backup_excludes(
    coresys: CoreSys,
    caplog: pytest.LogCaptureFixture,
    filename: str,
    exclude_db: bool,
    expect_excluded: bool,
    subfolder: str | None,
):
    """Test excludes in backup."""
    parent = coresys.config.path_muthurcommand
    if subfolder:
        test_path = PurePath(subfolder, filename)
        parent = coresys.config.path_muthurcommand / subfolder
        parent.mkdir(parents=True)
    else:
        test_path = PurePath(filename)

    (parent / filename).touch()

    backup = Backup(coresys, coresys.config.path_backup / "test.tar", "test", None)
    backup.new("test", utcnow().isoformat(), BackupType.PARTIAL)
    async with backup.create():
        with (
            patch.object(MuthurCommand, "begin_backup"),
            patch.object(MuthurCommand, "end_backup"),
            caplog.at_level(logging.DEBUG, logger="supervisor.muthurcommand.module"),
        ):
            await backup.store_muthurcommand(exclude_database=exclude_db)

    assert (
        f"Ignoring data/{test_path.as_posix()} because of " in caplog.text
    ) is expect_excluded
