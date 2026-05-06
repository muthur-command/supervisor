"""Fixup that recovers the MC application stack via an ordered restart.

Pairs with ``CheckMCStackDown``: when the resolution centre raises a
``MC_STACK_DOWN`` issue, the operator can click the
``EXECUTE_RESTART`` suggestion (or call the
``/resolution/suggestion/<uuid>`` endpoint) and the Supervisor will run
``MCStack.restart()`` — postgres → redis → mc_bd → mc_fd, in order, with
the documented per-component timeouts. PostgreSQL / Redis bind-mount
volumes are *never* removed by this fixup.
"""

from __future__ import annotations

import logging

from ...coresys import CoreSys
from ...exceptions import MCStackError, ResolutionFixupError
from ..const import ContextType, IssueType, SuggestionType
from .base import FixupBase

_LOGGER: logging.Logger = logging.getLogger(__name__)


def setup(coresys: CoreSys) -> FixupBase:
    """Fixup setup function."""
    return FixupMCStackExecuteRestart(coresys)


class FixupMCStackExecuteRestart(FixupBase):
    """Run ``MCStack.restart`` to recover a degraded stack."""

    async def process_fixup(self, reference: str | None = None) -> None:
        """Execute the ordered MC stack restart."""
        try:
            await self.sys_mc_stack.restart()
        except MCStackError as err:
            _LOGGER.error("MC stack restart fixup failed: %s", err)
            raise ResolutionFixupError() from err

    @property
    def suggestion(self) -> SuggestionType:
        """Return suggestion enum this fixup handles."""
        return SuggestionType.EXECUTE_RESTART

    @property
    def context(self) -> ContextType:
        """Return the context the fixup applies to."""
        return ContextType.MC_STACK

    @property
    def issues(self) -> list[IssueType]:
        """Return issue types that this fixup may dismiss on success."""
        return [IssueType.MC_STACK_DOWN]
