"""Private media source serving staged AI Task attachments."""

from __future__ import annotations

from typing import override

from homeassistant.components.media_player.const import MediaClass
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant

from .attachments import staged_registry
from .const import DOMAIN


async def async_get_media_source(hass: HomeAssistant) -> TextReaderMediaSource:
    """Set up the Text Reader media source."""
    return TextReaderMediaSource(hass)


class TextReaderMediaSource(MediaSource):
    """Resolves live staging tokens to local files; never browsable."""

    name = "Text Reader"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise."""
        super().__init__(DOMAIN)
        self.hass = hass

    @override
    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve a live token to its staged file."""
        staged = staged_registry(self.hass).get(item.identifier)
        if staged is None:
            raise Unresolvable("Unknown or expired attachment")
        path, mime = staged
        return PlayMedia(url="", mime_type=mime, path=path)

    @override
    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """List nothing: staged images are short-lived, internal and private.

        HA's root browser lists every source, so return an empty directory
        rather than an error. The title is the only text field HA offers.
        """
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=None,
            title="Text Reader",
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            can_play=False,
            can_expand=True,
            children=[],
            thumbnail=None,
        )
