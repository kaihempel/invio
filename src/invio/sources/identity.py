"""When two configured sources are the same source.

Shared by source discovery (dropping duplicate findings) and the job service (refusing to
append a source a job already has), so both apply one rule.
"""

from invio.config.job import (
    SourceConfig,
    YoutubeChannelSource,
    YoutubePlaylistSource,
    parse_youtube_channel,
    parse_youtube_playlist,
)
from invio.sources.urls import canonical_url

__all__ = ["source_identity"]


def source_identity(source: SourceConfig) -> tuple[str, str]:
    """``(type, locator)``: the canonical URL, or the parsed channel or playlist id.

    A YouTube handle compares case-insensitively; a channel id, a playlist id and a URL path
    do not.
    """
    if isinstance(source, YoutubeChannelSource):
        kind, token = parse_youtube_channel(source.channel_id)
        return source.type, f"{kind}:{token.lower() if kind == 'handle' else token}"
    if isinstance(source, YoutubePlaylistSource):
        return source.type, parse_youtube_playlist(source.playlist_id)
    return source.type, canonical_url(str(source.url))
