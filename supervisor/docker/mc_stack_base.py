"""Shared building blocks for MC application stack Docker wrappers.

Every component of the MC stack (PostgreSQL, Redis, ``mc_bd``, ``mc_fd``)
uses the same network alias / restart-policy / label conventions so that
``DockerMonitor``, the future Observer plug-in and external monitoring
tools can filter stack containers in one shot. Centralising these
defaults here keeps the four ``Docker*`` classes from drifting apart.
"""

from __future__ import annotations

from typing import Final

from ..const import (
    DOCKER_NETWORK,
    LABEL_MC_MANAGED_BY,
    LABEL_MC_ROLE,
    LABEL_MC_STACK,
    MC_STACK_MANAGED_BY,
    MC_STACK_NAME,
)
from .const import RestartPolicy

# All stack containers should auto-restart with the daemon but respect a
# manual ``docker stop`` (so Supervisor's ``MCStack.stop`` truly stops
# them). ``unless-stopped`` is the policy that matches that semantics.
MC_STACK_RESTART_POLICY: Final[dict[str, RestartPolicy]] = {
    "Name": RestartPolicy.UNLESS_STOPPED,
}


def mc_stack_labels(role: str) -> dict[str, str]:
    """Return the label set applied to every MC stack container.

    ``role`` should be one of the ``MC_ROLE_*`` constants (``postgres`` /
    ``redis`` / ``backend`` / ``frontend``). The base ``LABEL_MANAGED``
    marker is added separately by ``DockerAPI._create_container_config``.
    """
    return {
        LABEL_MC_STACK: MC_STACK_NAME,
        LABEL_MC_ROLE: role,
        LABEL_MC_MANAGED_BY: MC_STACK_MANAGED_BY,
    }


def mc_stack_networking_config(*aliases: str) -> dict[str, dict[str, dict]]:
    """Build a ``NetworkingConfig`` payload attached to the ``mcos`` network.

    Multiple aliases can be supplied so containers are reachable under both
    the underscore (``mc_postgres``) and hyphen (``mc-postgres``) variants
    used across the Muthur Command stack and DNS templates.
    """
    return {
        "EndpointsConfig": {
            DOCKER_NETWORK: {"Aliases": list(aliases)},
        }
    }
