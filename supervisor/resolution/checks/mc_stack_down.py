"""Detect that the MC application stack is down while it should be running.

Stage 4 of the A1 plan asks for a stable signal that the MC stack is up
end-to-end. ``MCStack.healthcheck`` produces per-component readiness; if
any component is degraded *and* the operator wants the stack to run
(``MCStack.boot=True``), surface an issue with a one-click "restart MC
stack" suggestion instead of letting the watchdog churn silently.
"""

from __future__ import annotations

import logging

from ...const import CoreState
from ...coresys import CoreSys
from ..const import ContextType, IssueType, SuggestionType
from .base import CheckBase

_LOGGER: logging.Logger = logging.getLogger(__name__)


def setup(coresys: CoreSys) -> CheckBase:
    """Check setup function."""
    return CheckMCStackDown(coresys)


class CheckMCStackDown(CheckBase):
    """Flag the MC stack as down when one or more components are unhealthy."""

    async def run_check(self) -> None:
        """Create the issue + restart suggestion when the stack is degraded."""
        if not await self.approve_check():
            return
        _LOGGER.warning("MC application stack reports as degraded")
        self.sys_resolution.create_issue(
            IssueType.MC_STACK_DOWN,
            ContextType.MC_STACK,
            suggestions=[SuggestionType.EXECUTE_RESTART],
        )

    async def approve_check(self, reference: str | None = None) -> bool:
        """Return True iff the stack is enabled, supposed to run, and degraded."""
        stack = self.sys_mc_stack
        if not stack.enabled or not stack.boot:
            return False

        snapshot = await stack.healthcheck()
        if not snapshot:
            return False
        return any(component.degraded for component in snapshot.values())

    @property
    def issue(self) -> IssueType:
        """Issue type emitted by this check."""
        return IssueType.MC_STACK_DOWN

    @property
    def context(self) -> ContextType:
        """Context the issue belongs to."""
        return ContextType.MC_STACK

    @property
    def states(self) -> list[CoreState]:
        """Run only after the system is up — startup races would be noisy."""
        return [CoreState.RUNNING]
