"""Tests for Muthur Command constants."""

from awesomeversion import AwesomeVersion

from supervisor.muthurcommand.const import LANDINGPAGE, is_landingpage


def test_is_landingpage_none_is_safe() -> None:
    """None must not be compared directly to LANDINGPAGE (raises AwesomeVersionCompareException)."""
    assert is_landingpage(None) is False


def test_is_landingpage_recognizes_placeholder() -> None:
    """Landingpage version is detected."""
    assert is_landingpage(LANDINGPAGE) is True
    assert is_landingpage(AwesomeVersion("2026.05.0")) is False
