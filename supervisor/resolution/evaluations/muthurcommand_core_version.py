"""Evaluation class for Core version."""

import logging

from awesomeversion import (
    AwesomeVersion,
    AwesomeVersionException,
    AwesomeVersionStrategy,
)

from ...const import CoreState
from ...coresys import CoreSys
from ...muthurcommand.const import LANDINGPAGE
from ..const import UnsupportedReason
from .base import EvaluateBase

_LOGGER: logging.Logger = logging.getLogger(__name__)


def setup(coresys: CoreSys) -> EvaluateBase:
    """Initialize evaluation-setup function."""
    return EvaluateMuthurCommandCoreVersion(coresys)


class EvaluateMuthurCommandCoreVersion(EvaluateBase):
    """Evaluate the Muthur Command Core version."""

    @property
    def reason(self) -> UnsupportedReason:
        """Return a UnsupportedReason enum."""
        return UnsupportedReason.MUTHURCOMMAND_CORE_VERSION

    @property
    def on_failure(self) -> str:
        """Return a string that is printed when self.evaluate is True."""
        return f"Muthur Command Core version '{self.sys_muthurcommand.version}' is more than 2 years old!"

    @property
    def states(self) -> list[CoreState]:
        """Return a list of valid states when this evaluation can run."""
        return [CoreState.RUNNING, CoreState.SETUP]

    async def evaluate(self) -> bool:
        """Run evaluation."""
        # Stage 6 of the A1 plan: MCOS variants that ship no Muthur Command
        # Core ("unused" slot in the version JSON) must not be flagged as
        # unsupported just because the legacy Core is missing.
        if self.sys_muthurcommand.unused:
            return False

        if not (current := self.sys_muthurcommand.version) or not (
            latest := self.sys_muthurcommand.latest_version
        ):
            return False

        # Skip evaluation for landingpage version
        if current == LANDINGPAGE:
            return False

        try:
            # We use the latest known version as reference instead of current date.
            # This is crucial because when update information refresh is disabled due to
            # unsupported Core version, using date would create a permanent unsupported state.
            # Even if the user updates to the last known version, the system would remain
            # unsupported in 4+ years. By using latest known version, updating Core to the
            # last known version makes the system supported again, allowing update refresh.
            #
            # Muthur Command uses CalVer versioning (2024.1, 2024.2, etc.) with monthly releases.
            # We consider versions more than 2 years behind as unsupported.
            if (
                latest.strategy != AwesomeVersionStrategy.CALVER
                or latest.year is None
                or latest.minor is None
            ):
                return True  # Invalid latest version format

            # Calculate 24 months back from latest version
            cutoff_month = int(latest.minor)
            cutoff_year = int(latest.year) - 2

            # Create cutoff version
            cutoff_version = AwesomeVersion(
                f"{cutoff_year}.{cutoff_month}",
                ensure_strategy=AwesomeVersionStrategy.CALVER,
            )

            # Compare current version with the cutoff
            return current < cutoff_version

        except (AwesomeVersionException, ValueError, IndexError) as err:
            # This is run regularly, avoid log spam by logging at debug level
            _LOGGER.debug(
                "Failed to parse Muthur Command version '%s' or latest version '%s': %s",
                current,
                latest,
                err,
            )
            # Consider non-parseable versions as unsupported
            return True
