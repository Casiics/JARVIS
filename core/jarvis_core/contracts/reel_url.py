"""Instagram reel references, shared by core (which accepts them from the deck) and content.

Only the canonical form crosses the core/content boundary, so the same reel always maps to the
same reference and nothing but a short code travels with it.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_SHORTCODE = re.compile(r"^[A-Za-z0-9_-]{5,40}$")
_REEL_HOSTS = frozenset({"instagram.com", "www.instagram.com", "m.instagram.com"})
_REEL_KINDS = frozenset({"reel", "reels", "p"})


class InvalidReelUrl(ValueError):
    pass


def canonical_reel_url(url: str) -> str:
    """Accept only Instagram reel or post URLs and return the canonical form.

    Query strings and fragments (tracking ids) are dropped, so the same reel always maps to
    the same reference.
    """
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or parts.hostname not in _REEL_HOSTS:
        raise InvalidReelUrl("only https Instagram URLs are accepted")
    if parts.username or parts.password or parts.port:
        raise InvalidReelUrl("credentials or ports in the URL are not accepted")
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) != 2 or segments[0] not in _REEL_KINDS:
        raise InvalidReelUrl("expected /reel/<code>/ or /p/<code>/")
    if not _SHORTCODE.fullmatch(segments[1]):
        raise InvalidReelUrl("invalid reel code")
    return f"https://www.instagram.com/reel/{segments[1]}/"
