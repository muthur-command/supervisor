"""Format image templates from Muthur Command OS ``version`` JSON (``images.*`` placeholders)."""

from __future__ import annotations

from string import Formatter


def format_version_image_template(
    template: str,
    *,
    arch: str,
    machine: str | None,
) -> str:
    """Fill ``{arch}`` / ``{machine}`` per P0 frozen placeholder rules."""
    fieldnames = {name for _, name, _, _ in Formatter().parse(template) if name}
    kwargs: dict[str, str] = {}
    if "arch" in fieldnames:
        kwargs["arch"] = arch
    if "machine" in fieldnames:
        kwargs["machine"] = machine or "default"
    return template.format(**kwargs)
