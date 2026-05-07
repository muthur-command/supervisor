"""Test Home Assistant core."""

import asyncio
from datetime import datetime, timedelta
from http import HTTPStatus
from unittest.mock import ANY, MagicMock, Mock, PropertyMock, call, patch

import aiodocker
from aiodocker.containers import DockerContainer
from awesomeversion import AwesomeVersion
import pytest
from time_machine import travel

from supervisor.const import CpuArch
from supervisor.coresys import CoreSys
from supervisor.docker.interface import DockerInterface
from supervisor.docker.manager import DockerAPI
from supervisor.docker.muthurcommand import DockerMuthurCommand
from supervisor.exceptions import (
    AudioUpdateError,
    DockerError,
    MuthurCommandCrashError,
    MuthurCommandError,
    MuthurCommandJobError,
)
from supervisor.muthurcommand.api import APIState
from supervisor.muthurcommand.core import MuthurCommandCore
from supervisor.muthurcommand.module import MuthurCommand
from supervisor.resolution.const import ContextType, IssueType
from supervisor.resolution.data import Issue
from supervisor.updater import Updater

from tests.common import AsyncIterator


async def test_update_fails_if_out_of_date(coresys: CoreSys):
    """Test update of Home Assistant fails when supervisor or plugin is out of date."""
    coresys.hardware.disk.get_disk_free_space = lambda x: 5000

    with (
        patch.object(
            type(coresys.supervisor), "need_update", new=PropertyMock(return_value=True)
        ),
        pytest.raises(MuthurCommandJobError),
    ):
        await coresys.muthurcommand.core.update()

    with (
        patch.object(
            type(coresys.plugins.audio),
            "need_update",
            new=PropertyMock(return_value=True),
        ),
        patch.object(
            type(coresys.plugins.audio), "update", side_effect=AudioUpdateError
        ),
        pytest.raises(MuthurCommandJobError),
    ):
        await coresys.muthurcommand.core.update()


async def test_install_landingpage_docker_ratelimit_error(
    coresys: CoreSys, capture_exception: Mock, caplog: pytest.LogCaptureFixture
):
    """Test install landing page fails due to docker ratelimit error."""
    coresys.security.force = True
    coresys.docker.images.pull.side_effect = [
        aiodocker.DockerError(HTTPStatus.TOO_MANY_REQUESTS, {"message": "ratelimit"}),
        AsyncIterator([{}]),
    ]

    with (
        patch.object(DockerMuthurCommand, "attach", side_effect=DockerError),
        patch.object(
            Updater,
            "image_muthurcommand",
            new=PropertyMock(return_value="muthurcommand"),
        ),
        patch.object(
            DockerInterface, "arch", new=PropertyMock(return_value=CpuArch.AMD64)
        ),
        patch("supervisor.muthurcommand.core.asyncio.sleep") as sleep,
    ):
        await coresys.muthurcommand.core.install_landingpage()
        sleep.assert_awaited_once_with(30)

    assert "Failed to install landingpage, retrying after 30sec" in caplog.text
    capture_exception.assert_not_called()
    assert (
        Issue(IssueType.DOCKER_RATELIMIT, ContextType.SYSTEM)
        in coresys.resolution.issues
    )
    assert "Unhandled exception:" not in caplog.text


@pytest.mark.parametrize(
    "err",
    [
        aiodocker.DockerError(HTTPStatus.INTERNAL_SERVER_ERROR, {"message": "fail"}),
        aiodocker.DockerError(HTTPStatus.NOT_FOUND, {"message": "missing"}),
    ],
)
async def test_install_landingpage_other_error(
    coresys: CoreSys,
    capture_exception: Mock,
    caplog: pytest.LogCaptureFixture,
    err: aiodocker.DockerError,
):
    """Test install landing page fails due to other error."""
    coresys.docker.images.inspect.side_effect = [err, MagicMock()]

    with (
        patch.object(DockerMuthurCommand, "attach", side_effect=DockerError),
        patch.object(
            Updater,
            "image_muthurcommand",
            new=PropertyMock(return_value="muthurcommand"),
        ),
        patch.object(
            DockerInterface, "arch", new=PropertyMock(return_value=CpuArch.AMD64)
        ),
        patch("supervisor.muthurcommand.core.asyncio.sleep") as sleep,
    ):
        await coresys.muthurcommand.core.install_landingpage()
        sleep.assert_awaited_once_with(30)

    assert "Failed to install landingpage, retrying after 30sec" in caplog.text
    capture_exception.assert_called_once_with(err)
    assert "Unhandled exception:" not in caplog.text


async def test_install_docker_ratelimit_error(
    coresys: CoreSys, capture_exception: Mock, caplog: pytest.LogCaptureFixture
):
    """Test install fails due to docker ratelimit error."""
    coresys.security.force = True
    coresys.docker.images.pull.side_effect = [
        aiodocker.DockerError(HTTPStatus.TOO_MANY_REQUESTS, {"message": "ratelimit"}),
        AsyncIterator([{}]),
    ]

    with (
        patch.object(MuthurCommandCore, "start"),
        patch.object(DockerMuthurCommand, "cleanup"),
        patch.object(
            Updater,
            "image_muthurcommand",
            new=PropertyMock(return_value="muthurcommand"),
        ),
        patch.object(
            Updater, "version_muthurcommand", new=PropertyMock(return_value="2022.7.3")
        ),
        patch.object(
            DockerInterface, "arch", new=PropertyMock(return_value=CpuArch.AMD64)
        ),
        patch("supervisor.muthurcommand.core.asyncio.sleep") as sleep,
    ):
        await coresys.muthurcommand.core.install()
        sleep.assert_awaited_once_with(30)

    assert "Error on Home Assistant installation. Retrying in 30sec" in caplog.text
    capture_exception.assert_not_called()
    assert (
        Issue(IssueType.DOCKER_RATELIMIT, ContextType.SYSTEM)
        in coresys.resolution.issues
    )
    assert "Unhandled exception:" not in caplog.text


@pytest.mark.parametrize(
    "err",
    [
        aiodocker.DockerError(HTTPStatus.INTERNAL_SERVER_ERROR, {"message": "fail"}),
        aiodocker.DockerError(HTTPStatus.NOT_FOUND, {"message": "missing"}),
    ],
)
async def test_install_other_error(
    coresys: CoreSys,
    capture_exception: Mock,
    caplog: pytest.LogCaptureFixture,
    err: aiodocker.DockerError,
):
    """Test install fails due to other error."""
    coresys.docker.images.inspect.side_effect = [err, MagicMock()]

    with (
        patch.object(MuthurCommandCore, "start"),
        patch.object(DockerMuthurCommand, "cleanup"),
        patch.object(
            Updater,
            "image_muthurcommand",
            new=PropertyMock(return_value="muthurcommand"),
        ),
        patch.object(
            Updater, "version_muthurcommand", new=PropertyMock(return_value="2022.7.3")
        ),
        patch.object(
            DockerInterface, "arch", new=PropertyMock(return_value=CpuArch.AMD64)
        ),
        patch("supervisor.muthurcommand.core.asyncio.sleep") as sleep,
    ):
        await coresys.muthurcommand.core.install()
        sleep.assert_awaited_once_with(30)

    assert "Error on Home Assistant installation. Retrying in 30sec" in caplog.text
    capture_exception.assert_called_once_with(err)
    assert "Unhandled exception:" not in caplog.text


@pytest.mark.parametrize(
    ("active_job", "expected_log"),
    [
        (None, "Home Assistant Core installation in progress"),
        (MagicMock(progress=45.0), "Downloading Home Assistant Core image, 45%"),
    ],
)
async def test_install_logs_progress_periodically(
    coresys: CoreSys,
    caplog: pytest.LogCaptureFixture,
    active_job: MagicMock | None,
    expected_log: str,
):
    """Test install logs progress periodically during image pull."""
    coresys.security.force = True
    coresys.docker.images.pull.return_value = AsyncIterator([{}])
    original_wait_for = asyncio.wait_for

    async def mock_wait_for(coro, *, timeout=None):
        """Immediately timeout for the progress log wait, pass through others."""
        if timeout == 15:
            coro.close()
            await asyncio.sleep(0)
            raise TimeoutError
        return await original_wait_for(coro, timeout=timeout)

    with (
        patch.object(MuthurCommandCore, "start"),
        patch.object(DockerMuthurCommand, "cleanup"),
        patch.object(
            Updater,
            "image_muthurcommand",
            new=PropertyMock(return_value="muthurcommand"),
        ),
        patch.object(
            Updater, "version_muthurcommand", new=PropertyMock(return_value="2022.7.3")
        ),
        patch.object(
            DockerInterface, "arch", new=PropertyMock(return_value=CpuArch.AMD64)
        ),
        patch("supervisor.muthurcommand.core.asyncio.wait_for", new=mock_wait_for),
        patch.object(
            DockerMuthurCommand,
            "active_job",
            new=PropertyMock(return_value=active_job),
        ),
    ):
        await coresys.muthurcommand.core.install()

    assert expected_log in caplog.text


@pytest.mark.parametrize(
    ("container_exc", "image_exc", "delete_calls"),
    [
        (aiodocker.DockerError(404, {"message": "missing"}), None, []),
        (
            None,
            aiodocker.DockerError(404, {"message": "missing"}),
            [call(force=True, v=True)],
        ),
        (None, None, [call(force=True, v=True)]),
    ],
)
@pytest.mark.usefixtures("path_extern")
async def test_start(
    coresys: CoreSys,
    container: DockerContainer,
    container_exc: aiodocker.DockerError | None,
    image_exc: aiodocker.DockerError | None,
    delete_calls: list[call],
):
    """Test starting Home Assistant."""
    coresys.docker.images.inspect.return_value = {"Id": "123"}
    coresys.docker.images.inspect.side_effect = image_exc
    container.id = "123"
    coresys.docker.containers.get.side_effect = container_exc

    with (
        patch.object(
            MuthurCommand,
            "version",
            new=PropertyMock(return_value=AwesomeVersion("2023.7.0")),
        ),
        patch.object(DockerAPI, "run", return_value=container.show.return_value) as run,
        patch.object(MuthurCommandCore, "_block_till_run") as block_till_run,
    ):
        await coresys.muthurcommand.core.start()

        block_till_run.assert_called_once()
        run.assert_called_once()
        assert (
            run.call_args.args[0]
            == "ghcr.io/muthur-command/amd64-muthurcommand-qemux86-64"
        )
        assert run.call_args.kwargs["tag"] == AwesomeVersion("2023.7.0")
        assert run.call_args.kwargs["name"] == "muthurcommand"
        assert run.call_args.kwargs["hostname"] == "muthurcommand"

    container.stop.assert_not_called()
    assert container.delete.call_args_list == delete_calls


@pytest.mark.usefixtures("path_extern")
async def test_start_existing_container(coresys: CoreSys, container: DockerContainer):
    """Test starting Home Assistant when container exists and is viable."""
    coresys.docker.images.inspect.return_value = {"Id": "123"}
    container.show.return_value["Image"] = "123"
    container.show.return_value["State"]["Status"] = "exited"
    container.show.return_value["State"]["Running"] = False

    with (
        patch.object(
            MuthurCommand,
            "version",
            new=PropertyMock(return_value=AwesomeVersion("2023.7.0")),
        ),
        patch.object(MuthurCommandCore, "_block_till_run") as block_till_run,
    ):
        await coresys.muthurcommand.core.start()
        block_till_run.assert_called_once()

    container.start.assert_called_once()
    container.stop.assert_not_called()
    container.delete.assert_not_called()
    coresys.docker.containers.create.assert_not_called()


@pytest.mark.parametrize("exists", [True, False])
async def test_stop(coresys: CoreSys, container: DockerContainer, exists: bool):
    """Test stoppping Home Assistant."""
    if exists:
        container.show.return_value["State"]["Status"] = "running"
        container.show.return_value["State"]["Running"] = True
    else:
        coresys.docker.containers.get.side_effect = aiodocker.DockerError(
            404, {"message": "missing"}
        )

    await coresys.muthurcommand.core.stop()

    container.delete.assert_not_called()
    if exists:
        container.stop.assert_called_once_with(t=260)
    else:
        container.stop.assert_not_called()


async def test_restart(coresys: CoreSys, container: DockerContainer):
    """Test restarting Home Assistant."""
    with patch.object(MuthurCommandCore, "_block_till_run") as block_till_run:
        await coresys.muthurcommand.core.restart()
        block_till_run.assert_called_once()

    container.restart.assert_called_once_with(t=260)
    container.stop.assert_not_called()


@pytest.mark.parametrize(
    "get_error",
    [
        aiodocker.DockerError(404, {"message": "missing"}),
        aiodocker.DockerError(500, {"message": "fail"}),
        None,
    ],
)
async def test_restart_failures(
    coresys: CoreSys,
    container: DockerContainer,
    get_error: aiodocker.DockerError | None,
):
    """Test restart fails when container missing or can't be restarted."""
    container.restart.side_effect = aiodocker.DockerError(500, {"message": "fail"})
    if get_error:
        coresys.docker.containers.get.side_effect = get_error

    with pytest.raises(MuthurCommandError):
        await coresys.muthurcommand.core.restart()


@pytest.mark.parametrize(
    "get_error,running",
    [
        (aiodocker.DockerError(404, {"message": "missing"}), False),
        (aiodocker.DockerError(500, {"message": "fail"}), False),
        (None, False),
        (None, True),
    ],
)
async def test_stats_failures(
    coresys: CoreSys,
    container: DockerContainer,
    get_error: aiodocker.DockerError | None,
    running: bool,
):
    """Test errors when getting stats."""
    container.show.return_value["State"]["Status"] = "running" if running else "stopped"
    container.show.return_value["State"]["Running"] = running
    container.stats.side_effect = aiodocker.DockerError(500, {"message": "fail"})
    if get_error:
        coresys.docker.containers.get.side_effect = get_error

    with pytest.raises(MuthurCommandError):
        await coresys.muthurcommand.core.stats()


async def test_api_check_timeout(
    coresys: CoreSys, container: DockerContainer, caplog: pytest.LogCaptureFixture
):
    """Test attempts to contact the API timeout."""
    container.show.return_value["State"]["Status"] = "stopped"
    container.show.return_value["State"]["Running"] = False
    coresys.muthurcommand.version = AwesomeVersion("2023.9.0")
    coresys.muthurcommand.api.get_api_state.return_value = None

    async def mock_instance_start(*_):
        container.show.return_value["State"]["Status"] = "running"
        container.show.return_value["State"]["Running"] = True

    with (
        patch.object(DockerMuthurCommand, "start", new=mock_instance_start),
        patch.object(DockerAPI, "container_is_initialized", return_value=True),
        travel(datetime(2023, 10, 2, 0, 0, 0), tick=False) as traveller,
    ):

        async def mock_sleep(*args):
            traveller.shift(timedelta(minutes=1))

        with (
            patch("supervisor.muthurcommand.core.asyncio.sleep", new=mock_sleep),
            pytest.raises(MuthurCommandCrashError),
        ):
            await coresys.muthurcommand.core.start()

    assert coresys.muthurcommand.api.get_api_state.call_count == 10
    assert (
        "No Home Assistant Core response, assuming a fatal startup error" in caplog.text
    )


async def test_api_check_success(
    coresys: CoreSys, container: DockerContainer, caplog: pytest.LogCaptureFixture
):
    """Test attempts to contact the API timeout."""
    container.show.return_value["State"]["Status"] = "stopped"
    container.show.return_value["State"]["Running"] = False
    coresys.muthurcommand.version = AwesomeVersion("2023.9.0")

    async def mock_instance_start(*_):
        container.show.return_value["State"]["Status"] = "running"
        container.show.return_value["State"]["Running"] = True

    with (
        patch.object(DockerMuthurCommand, "start", new=mock_instance_start),
        patch.object(DockerAPI, "container_is_initialized", return_value=True),
        travel(datetime(2023, 10, 2, 0, 0, 0), tick=False) as traveller,
    ):

        async def mock_sleep(*args):
            traveller.shift(timedelta(minutes=1))

        with patch("supervisor.muthurcommand.core.asyncio.sleep", new=mock_sleep):
            await coresys.muthurcommand.core.start()

    assert coresys.muthurcommand.api.get_api_state.call_count == 1
    assert "Detect a running Home Assistant instance" in caplog.text


async def test_api_check_database_migration(
    coresys: CoreSys, container: DockerContainer, caplog: pytest.LogCaptureFixture
):
    """Test attempts to contact the API timeout."""
    calls = []

    def mock_api_state(*args):
        calls.append(None)
        if len(calls) > 50:
            return APIState("RUNNING", False)
        else:
            return APIState("NOT_RUNNING", True)

    container.show.return_value["State"]["Status"] = "stopped"
    container.show.return_value["State"]["Running"] = False
    coresys.muthurcommand.version = AwesomeVersion("2023.9.0")
    coresys.muthurcommand.api.get_api_state.side_effect = mock_api_state

    async def mock_instance_start(*_):
        container.show.return_value["State"]["Status"] = "running"
        container.show.return_value["State"]["Running"] = True

    with (
        patch.object(DockerMuthurCommand, "start", new=mock_instance_start),
        patch.object(DockerAPI, "container_is_initialized", return_value=True),
        travel(datetime(2023, 10, 2, 0, 0, 0), tick=False) as traveller,
    ):

        async def mock_sleep(*args):
            traveller.shift(timedelta(minutes=1))

        with patch("supervisor.muthurcommand.core.asyncio.sleep", new=mock_sleep):
            await coresys.muthurcommand.core.start()

    assert coresys.muthurcommand.api.get_api_state.call_count == 51
    assert "Detect a running Home Assistant instance" in caplog.text


async def test_core_loads_wrong_image_for_machine(
    coresys: CoreSys, container: DockerContainer
):
    """Test core is loaded with wrong image for machine."""
    coresys.muthurcommand.set_image(
        "ghcr.io/muthur-command/aarch64-muthurcommand-odroid-n2"
    )
    coresys.muthurcommand.version = AwesomeVersion("2024.4.0")

    with patch.object(
        DockerAPI,
        "pull_image",
        return_value={
            "Id": "abc123",
            "Config": {"Labels": {"io.mcio.version": "2024.4.0"}},
        },
    ) as pull_image:
        container.show.return_value |= pull_image.return_value
        await coresys.muthurcommand.core.load()
        pull_image.assert_called_once_with(
            ANY,
            "ghcr.io/muthur-command/amd64-muthurcommand-qemux86-64",
            "2024.4.0",
            platform="linux/amd64",
            auth=None,
        )

    container.delete.assert_called_once_with(force=True, v=True)
    assert coresys.docker.images.delete.call_args_list[0] == call(
        "ghcr.io/muthur-command/aarch64-muthurcommand-odroid-n2:latest",
        force=True,
    )
    assert coresys.docker.images.delete.call_args_list[1] == call(
        "ghcr.io/muthur-command/aarch64-muthurcommand-odroid-n2:2024.4.0",
        force=True,
    )
    assert (
        coresys.muthurcommand.image
        == "ghcr.io/muthur-command/amd64-muthurcommand-qemux86-64"
    )


async def test_core_load_allows_image_override(
    coresys: CoreSys, container: DockerContainer
):
    """Test core does not change image if user overrode it."""
    coresys.muthurcommand.set_image(
        "ghcr.io/muthur-command/aarch64-muthurcommand-odroid-n2"
    )
    coresys.muthurcommand.version = AwesomeVersion("2024.4.0")
    container.show.return_value["Config"] = {"Labels": {"io.mcio.version": "2024.4.0"}}

    coresys.muthurcommand.override_image = True
    await coresys.muthurcommand.core.load()

    container.delete.assert_not_called()
    coresys.docker.images.delete.assert_not_called()
    coresys.docker.images.inspect.assert_not_called()
    assert (
        coresys.muthurcommand.image
        == "ghcr.io/muthur-command/aarch64-muthurcommand-odroid-n2"
    )


async def test_core_loads_wrong_image_for_architecture(
    coresys: CoreSys, container: DockerContainer
):
    """Test core is loaded with wrong image for architecture."""
    coresys.muthurcommand.version = AwesomeVersion("2024.4.0")
    coresys.docker.images.inspect.return_value = img_data = (
        coresys.docker.images.inspect.return_value
        | {
            "Architecture": "arm64",
            "Config": {"Labels": {"io.mcio.version": "2024.4.0"}},
        }
    )
    container.show.return_value |= img_data

    with patch.object(
        DockerAPI,
        "pull_image",
        return_value=img_data | {"Architecture": "amd64"},
    ) as pull_image:
        await coresys.muthurcommand.core.load()
        pull_image.assert_called_once_with(
            ANY,
            "ghcr.io/muthur-command/amd64-muthurcommand-qemux86-64",
            "2024.4.0",
            platform="linux/amd64",
            auth=None,
        )

    container.delete.assert_called_once_with(force=True, v=True)
    assert coresys.docker.images.delete.call_args_list[0] == call(
        "ghcr.io/muthur-command/amd64-muthurcommand-qemux86-64:latest",
        force=True,
    )
    assert coresys.docker.images.delete.call_args_list[1] == call(
        "ghcr.io/muthur-command/amd64-muthurcommand-qemux86-64:2024.4.0",
        force=True,
    )
    assert (
        coresys.muthurcommand.image
        == "ghcr.io/muthur-command/amd64-muthurcommand-qemux86-64"
    )
