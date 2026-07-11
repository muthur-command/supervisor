"""Constants for the add-on store."""

from enum import StrEnum
from pathlib import Path

from ..const import REPOSITORY_CORE, REPOSITORY_LOCAL, SUPERVISOR_DATA, URL_MCOS_ADDONS

FILE_MCOS_STORE = Path(SUPERVISOR_DATA, "store.json")
"""Repository type definitions for the store."""

# Legacy store URLs folded into the built-in core repository.
REPOSITORY_URL_MIGRATIONS: dict[str, str] = {
    "https://github.com/mcos-addons/addons-repository": REPOSITORY_CORE,
    URL_MCOS_ADDONS: REPOSITORY_CORE,
}


class BuiltinRepository(StrEnum):
    """All built-in repositories that come pre-configured."""

    # Local repository (non-git, special handling)
    LOCAL = REPOSITORY_LOCAL

    # Git-based built-in repositories
    CORE = REPOSITORY_CORE
    ESPHOME = "https://github.com/esphome/home-assistant-addon"
    MUSIC_ASSISTANT = "https://github.com/music-assistant/home-assistant-addon"

    @property
    def git_url(self) -> str:
        """Return the git URL for this repository."""
        if self == BuiltinRepository.LOCAL:
            raise RuntimeError("Local repository does not have a git URL")
        if self == BuiltinRepository.CORE:
            return URL_MCOS_ADDONS
        else:
            return self.value  # For URL-based repos, value is the URL
