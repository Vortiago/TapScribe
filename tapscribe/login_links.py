"""Single-use login links, and the dashboard session cookies they mint.

A login link is spent once, for a cookie: the tray reads `.auth-password` off
disk, asks for a link, and opens it, so "Open dashboard" lands the operator on a
dashboard that is already signed in and the browser's native Basic dialog never
appears (ADR-0023). The cookie is a second credential FORM for ADR-0008's BASIC
scheme, not a fourth scheme — `auth.basic_auth_middleware` accepts it exactly
where it accepts an `Authorization: Basic` header, and no route is gated
differently by which of the two a caller used.

Everything here is in memory and per-process, deliberately: a Recorder restart
logs the browser out, which costs one tray click and buys no third secret at rest
beside `.auth-password` and `.tap-token`. It is also DOM-free, HTTP-free and
FastAPI-free, so the state machine below is unit-tested directly rather than
through a client.

The store hangs off `app.state.login_links` (built in `lifespan`) rather than
being a module global: per-app means tests never leak one another's cookies, and
"dies with the process" becomes a property of the object rather than a promise.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from .auth import utf8_compare_digest

#: How long an unspent link stays live. Single-use is the primary bound; this
#: covers the case single-use does not — a token that is never SPENT (no default
#: browser, a launch that failed, a cancelled click) would otherwise be a live
#: credential with no expiry. It also travels through channels the password does
#: not: the address bar, and the OS's open-URL handoff, both of which can log it,
#: where `.auth-password` sits in one file behind file permissions. The tray
#: opens the browser immediately, so mint-to-spend is sub-second.
TOKEN_TTL_S: float = 60.0

#: How long after the first spend the SAME token still answers with the SAME
#: cookie. A link scanner, a terminal's URL preview or a double-click otherwise
#: spends the token before the operator's real navigation lands, and the operator
#: is shown a dead-link page for a link they just made. Re-issuing the session it
#: already issued grants nothing new — the cookie exists either way — so the
#: window costs nothing beyond its own length.
GRACE_S: float = 10.0


@dataclass
class _Link:
    """One minted link. `cookie` is None until it is spent, and is the session it
    issued afterwards — which is what makes a re-spend inside the grace window
    idempotent rather than a second login."""

    expires_at: float
    cookie: str | None = None
    spent_at: float = 0.0


@dataclass
class LoginLinks:
    """The live links and the sessions they have issued.

    Not thread-safe by design: every caller is a request handler on one event
    loop, and the middleware's read is the only hot path. If that ever stops
    being true, the lock goes here, not at the call sites.
    """

    _links: dict[str, _Link] = field(default_factory=dict)
    _cookies: set[str] = field(default_factory=set)
    #: Injected so the tests drive expiry without sleeping. `time.monotonic` and
    #: not `time.time`: a clock step (NTP, a laptop waking) must not retire a
    #: live link or resurrect a dead one.
    _now: object = field(default=time.monotonic)

    def mint(self) -> str:
        """A fresh single-use token. Requires nothing — the CALLER is what
        requires the password, since `POST /api/login-link` is Basic-gated, so
        only somebody who could already reach the dashboard can mint one."""
        self._sweep()
        token = secrets.token_urlsafe(32)
        self._links[token] = _Link(expires_at=self._clock() + TOKEN_TTL_S)
        return token

    def spend(self, token: str) -> str | None:
        """Trade a token for the session cookie it issues, or None if it is
        unknown, expired, or spent longer than `GRACE_S` ago.

        None is the whole failure surface on purpose: which of the three it was
        is not something the route should tell an unauthenticated caller, and it
        is not something the operator can act on differently — the fix is the
        same "get a fresh one from the tray" either way.
        """
        self._sweep()
        found = self._find(token)
        if found is None:
            return None

        now = self._clock()
        link = self._links[found]
        if link.cookie is None:
            link.cookie = secrets.token_urlsafe(32)
            link.spent_at = now
            self._cookies.add(link.cookie)
            return link.cookie

        # Already spent. Inside the grace window this is the scanner/double-click
        # case and answers with the session it already issued; outside it, the
        # link is used up.
        if now - link.spent_at <= GRACE_S:
            return link.cookie
        return None

    def validate(self, cookie: str | None) -> bool:
        """Whether `cookie` is a session this store issued. Compared in constant
        time against every live session, like every other credential check in the
        Recorder — a dict/set lookup would answer in a length- and
        content-dependent time."""
        if not cookie:
            return False
        return any(utf8_compare_digest(cookie, issued) for issued in tuple(self._cookies))

    def _find(self, token: str) -> str | None:
        """The stored token equal to `token`, compared in constant time. Returns
        the STORED string so the caller indexes the dict with a value it already
        held, never with attacker-supplied text."""
        if not token:
            return None
        for known in tuple(self._links):
            if utf8_compare_digest(token, known):
                return known
        return None

    def _sweep(self) -> None:
        """Drop links that can no longer answer: expired unspent ones, and spent
        ones past their grace. Swept on touch rather than on a timer, the way
        `transcribers`' idle sweep is, so there is no background task to own.

        Issued COOKIES are not swept — they are the browser's session and live as
        long as the process does. Their number is bounded by how many times the
        operator has signed in, and minting requires the password.
        """
        now = self._clock()
        dead = [
            token
            for token, link in self._links.items()
            if (link.cookie is None and now >= link.expires_at)
            or (link.cookie is not None and now - link.spent_at > GRACE_S)
        ]
        for token in dead:
            del self._links[token]

    def _clock(self) -> float:
        return float(self._now())  # type: ignore[operator]
