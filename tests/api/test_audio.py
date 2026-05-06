"""Test audio api."""

from supervisor.host.const import LogFormatter


async def test_api_audio_logs(advanced_logs_tester) -> None:
    """Test audio logs."""
    await advanced_logs_tester("/audio", "mcio_audio", LogFormatter.VERBOSE)
