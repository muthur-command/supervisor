"""Evaluate MC application stack versions.

Stage 6 of the A1 plan: surface stale ``mc_bd`` / ``mc_fd`` deployments
through the resolution centre instead of the HA-Core-only evaluator. We
only flag the *application* halves of the stack (mc_bd / mc_fd) — the
data-plane components (PostgreSQL, Redis) are pinned by major in the
version JSON and never get a "too old" warning automatically.
"""

from __future__ import annotations

import logging

from awesomeversion import (
    AwesomeVersion,
    AwesomeVersionException,
    AwesomeVersionStrategy,
)

from ...const import CoreState
from ...coresys import CoreSys
from ..const import UnsupportedReason
from .base import EvaluateBase

_LOGGER: logging.Logger = logging.getLogger(__name__)

# Application-half components that follow CalVer / SemVer release cadence
# and warrant a staleness check.
_APP_COMPONENT_KEYS = ("mc_bd", "mc_fd")

# CalVer cutoff: warn when current is more than this many months behind
# the latest known version. Mirrors the HA Core evaluator's 2-year
# heuristic but tightened to 12 months because the MC stack moves faster.
_CALVER_CUTOFF_MONTHS = 12


def setup(coresys: CoreSys) -> EvaluateBase:
    """Initialize evaluation-setup function."""
    return EvaluateMCStackVersion(coresys)


class EvaluateMCStackVersion(EvaluateBase):
    """Evaluate whether the running MC stack is too old."""

    @property
    def reason(self) -> UnsupportedReason:
        """Return the unsupported reason emitted on failure."""
        return UnsupportedReason.MC_STACK_VERSION

    @property
    def on_failure(self) -> str:
        """Return the user-visible message when this eval flags."""
        return (
            "MC application stack is more than "
            f"{_CALVER_CUTOFF_MONTHS} months behind the latest release."
        )

    @property
    def states(self) -> list[CoreState]:
        """Return CoreStates in which this eval is meaningful."""
        return [CoreState.RUNNING, CoreState.SETUP]

    async def evaluate(self) -> bool:
        """Return True if the running MC stack is unsupported."""
        # Only run when the operator opted into the MC stack at all.
        if not self.sys_mc_stack.enabled:
            return False

        for key, current, latest in self._iter_app_components():
            if current is None or latest is None:
                # If either side is missing we can't compare. Don't flag —
                # the missing image template is its own bug surfaced by
                # ``MCStack.enabled``.
                continue
            if self._is_stale(current, latest):
                _LOGGER.debug(
                    "MC stack component '%s' running %s, latest %s — flagged",
                    key,
                    current,
                    latest,
                )
                return True
        return False

    def _iter_app_components(
        self,
    ) -> list[tuple[str, AwesomeVersion | None, AwesomeVersion | None]]:
        """Yield (key, current, latest) for every application-half component."""
        updater = self.sys_updater
        return [
            (
                "mc_bd",
                self.sys_mc_stack.backend.version,
                updater.version_mc_bd,
            ),
            (
                "mc_fd",
                self.sys_mc_stack.frontend.version,
                updater.version_mc_fd,
            ),
        ]

    @staticmethod
    def _calver_components(
        current: AwesomeVersion, latest: AwesomeVersion
    ) -> tuple[int, int, int, int] | None:
        """Return normalized CalVer (year, month) pairs for current/latest."""
        if latest.strategy != AwesomeVersionStrategy.CALVER:
            return None
        if current.strategy != AwesomeVersionStrategy.CALVER:
            return None
        if latest.year is None or latest.minor is None:
            return None
        if current.year is None or current.minor is None:
            return None
        return (
            int(latest.year),
            int(latest.minor),
            int(current.year),
            int(current.minor),
        )

    @staticmethod
    def _is_stale(current: AwesomeVersion, latest: AwesomeVersion) -> bool:
        """Return True if ``current`` is more than the cutoff behind ``latest``."""
        try:
            # CalVer (year.month.patch): compare year/month rolled into a
            # single month index so we don't fight December/January.
            if calver := EvaluateMCStackVersion._calver_components(current, latest):
                latest_year, latest_month, current_year, current_month = calver
                latest_idx = latest_year * 12 + latest_month
                current_idx = current_year * 12 + current_month
                return latest_idx - current_idx > _CALVER_CUTOFF_MONTHS

            # Otherwise fall back to a simple "current < latest" — that
            # only flags when there's an actual newer release available.
            return current < latest
        except (AwesomeVersionException, TypeError, ValueError) as err:
            _LOGGER.debug("MC stack version compare failed: %s", err)
            return False
