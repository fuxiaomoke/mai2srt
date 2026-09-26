"""Identity every outbound model request carries.

The standard-library default User-Agent (``Python-urllib/3.x``) is rejected by
the WAF in front of some relay stations with a bare ``403 Forbidden`` -- which
in the UI is indistinguishable from a wrong key or an unavailable model. Real
browsers and every normal SDK send a User-Agent, so an app that omits it looks
like a scraper.

Keep ALL outbound model traffic on this constant: a call site that misses it
works against official endpoints and fails only on those relays, which is the
hardest kind of failure to diagnose from a bug report.
"""
from __future__ import annotations

from . import __version__

USER_AGENT = "mai2srt/%s" % __version__
