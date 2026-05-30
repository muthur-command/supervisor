"""Supervisor REST API endpoints for the MC application stack.

Stage 4 of the A1 plan: expose the four-container stack to operators so
they can inspect versions, kick off a coordinated restart and trigger an
upgrade. Per-component endpoints live under ``/mc_stack/{name}`` to keep
the routing flat and consistent with the per-plug-in style.

Stage 5 contributes ``/mc_stack/health`` (component-level readiness probe
output) and stage 6 wires ``/mc_fd/web/{path:.*}`` so the Supervisor can
proxy the ``mc_fd`` login page when the operator only has access to the
Supervisor port.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
from aiohttp import ClientTimeout, hdrs, web
from aiohttp.web_exceptions import HTTPBadGateway, HTTPServiceUnavailable
import voluptuous as vol

from ..const import (
    ATTR_BOOT,
    ATTR_ENABLED,
    ATTR_HEALTHY,
    ATTR_IMAGE,
    ATTR_MC_BD,
    ATTR_MC_FD,
    ATTR_NAME,
    ATTR_POSTGRESQL,
    ATTR_REDIS,
    ATTR_STATE,
    ATTR_VERSION,
    ATTR_VERSION_LATEST,
    ATTR_WATCHDOG,
    MC_FRONTEND_PORT,
)
from ..coresys import CoreSysAttributes
from ..docker.interface import DockerInterface
from ..exceptions import APIError, MCStackError
from .utils import api_process, api_validate

SCHEMA_OPTIONS = vol.Schema(
    {
        vol.Optional(ATTR_BOOT): bool,
        vol.Optional(ATTR_WATCHDOG): bool,
    }
)

_LOGGER: logging.Logger = logging.getLogger(__name__)


class APIMCStack(CoreSysAttributes):
    """Handle REST API for the MC application stack."""

    async def _component_state(self, inst: DockerInterface) -> str:
        """Return the current container state name (or ``unknown``)."""
        return str(await inst.current_state())

    async def _component_payload(self, inst: DockerInterface) -> dict[str, Any]:
        """Build the JSON payload for a single component."""
        latest_version: str | None = None
        for prop_name in (
            "version_mc_bd",
            "version_mc_fd",
            "version_postgresql",
            "version_redis",
        ):
            value = getattr(self.sys_updater, prop_name, None)
            if value is None:
                continue
            if inst.image and prop_name.split("_", 1)[1] in inst.image:
                latest_version = str(value)
                break

        return {
            ATTR_NAME: inst.name,
            ATTR_IMAGE: inst.image,
            ATTR_VERSION: str(inst.version) if inst.version else None,
            ATTR_VERSION_LATEST: latest_version,
            ATTR_STATE: await self._component_state(inst),
        }

    @api_process
    async def info(self, request: web.Request) -> dict[str, Any]:
        """Return MC stack overview (per-component versions and state)."""
        stack = self.sys_mc_stack
        return {
            ATTR_ENABLED: stack.enabled,
            ATTR_BOOT: stack.boot,
            ATTR_WATCHDOG: stack.watchdog,
            ATTR_POSTGRESQL: await self._component_payload(stack.postgres),
            ATTR_REDIS: await self._component_payload(stack.redis),
            ATTR_MC_BD: await self._component_payload(stack.backend),
            ATTR_MC_FD: await self._component_payload(stack.frontend),
        }

    @api_process
    async def options(self, request: web.Request) -> None:
        """Update MC stack runtime options (boot, watchdog)."""
        body = await api_validate(SCHEMA_OPTIONS, request)
        stack = self.sys_mc_stack
        if ATTR_BOOT in body:
            stack.boot = body[ATTR_BOOT]
        if ATTR_WATCHDOG in body:
            stack.watchdog = body[ATTR_WATCHDOG]
        await stack.config.save_data()

    @api_process
    async def start(self, request: web.Request) -> None:
        """Start the whole MC stack (in dependency order)."""
        try:
            await self.sys_mc_stack.start()
        except MCStackError as err:
            raise APIError(str(err)) from err

    @api_process
    async def stop(self, request: web.Request) -> None:
        """Stop the whole MC stack (reverse dependency order)."""
        try:
            await self.sys_mc_stack.stop()
        except MCStackError as err:
            raise APIError(str(err)) from err

    @api_process
    async def restart(self, request: web.Request) -> None:
        """Restart the whole MC stack."""
        try:
            await self.sys_mc_stack.restart()
        except MCStackError as err:
            raise APIError(str(err)) from err

    @api_process
    async def update(self, request: web.Request) -> None:
        """Update components whose desired version changed."""
        try:
            await self.sys_mc_stack.update()
        except MCStackError as err:
            raise APIError(str(err)) from err

    @api_process
    async def health(self, request: web.Request) -> dict[str, Any]:
        """Return per-component application-level health snapshot."""
        snapshot = await self.sys_mc_stack.healthcheck()
        return {
            name: {
                ATTR_NAME: comp.name,
                ATTR_STATE: str(comp.state),
                ATTR_HEALTHY: comp.healthy,
            }
            for name, comp in snapshot.items()
        }

    async def proxy_frontend(
        self, request: web.Request
    ) -> web.Response | web.StreamResponse:
        """Proxy ``/mc_fd/web/{path}`` straight to the ``mc_fd`` container.

        Stage 6 acceptance: "Ingress 能打开 mc_fd 登录页" — when the
        operator only has access to the Supervisor port (e.g. through
        the host's MCOS gateway), this passthrough lets them reach the
        ``mc_fd`` login page without exposing it on its own host port.
        """
        if not self.sys_mc_stack.enabled:
            raise HTTPServiceUnavailable(reason="MC stack disabled")

        path = request.match_info.get("path", "")
        url = f"http://mc_fd:{MC_FRONTEND_PORT}/{path}"
        if request.query_string:
            url = f"{url}?{request.query_string}"

        # Strip hop-by-hop headers and any Supervisor auth headers; the
        # MC stack is reachable on the internal ``mcio`` network and
        # mc_fd should never see Supervisor's bearer tokens.
        headers: dict[str, str] = {}
        for key, value in request.headers.items():
            if key.lower() in {
                hdrs.CONNECTION.lower(),
                hdrs.CONTENT_LENGTH.lower(),
                hdrs.CONTENT_ENCODING.lower(),
                hdrs.TRANSFER_ENCODING.lower(),
                hdrs.UPGRADE.lower(),
                "x-mcio-key",
                "x-supervisor-token",
            }:
                continue
            headers[key] = value

        try:
            async with self.sys_websession.request(
                request.method,
                url,
                headers=headers,
                params=request.query,
                allow_redirects=False,
                data=await request.read(),
                timeout=ClientTimeout(total=None),
                skip_auto_headers={hdrs.CONTENT_TYPE},
            ) as upstream:
                response = web.StreamResponse(
                    status=upstream.status,
                    headers={
                        k: v
                        for k, v in upstream.headers.items()
                        if k.lower()
                        not in {
                            hdrs.TRANSFER_ENCODING.lower(),
                            hdrs.CONTENT_LENGTH.lower(),
                            hdrs.CONTENT_ENCODING.lower(),
                        }
                    },
                )
                if maybe_ct := upstream.headers.get(hdrs.CONTENT_TYPE):
                    response.content_type = (maybe_ct.partition(";"))[0].strip()
                await response.prepare(request)
                async for chunk, _ in upstream.content.iter_chunks():
                    await response.write(chunk)
                return response
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("mc_fd proxy error to %s: %s", url, err)
            raise HTTPBadGateway() from err
