"""Fixtures for Muthur Command tests."""

from awesomeversion import AwesomeVersion
import pytest

from supervisor.coresys import CoreSys


@pytest.fixture
def ha_core_configured(coresys: CoreSys) -> None:
    """Pretend Muthur Command Core is installed (defeats the ``unused`` short-circuit).

    The default ``coresys`` fixture leaves both ``version`` and
    ``latest_version`` unset, which the websocket now treats as "MCOS
    image with no Muthur Command Core" (Stage 5). Tests that exercise
    the legacy "Core is configured but unreachable" branch need an
    explicit version.
    """
    coresys.muthurcommand.version = AwesomeVersion("2024.1.0")
    coresys.updater._data["muthurcommand"] = AwesomeVersion("2024.1.0")  # noqa: SLF001
