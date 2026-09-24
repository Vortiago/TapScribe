"""The login-link store: single use, a grace window, and an expiry.

Driven directly, with no HTTP and no app — the point of keeping the state
machine in its own module (ADR-0023). The clock is injected, so nothing here
sleeps.
"""

from __future__ import annotations

from tapscribe.login_links import (
    GRACE_S,
    SESSION_IDLE_S,
    SESSION_MAX_S,
    TOKEN_TTL_S,
    LoginLinks,
)


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def store() -> tuple[LoginLinks, Clock]:
    clock = Clock()
    return LoginLinks(_now=clock), clock


def test_a_minted_link_spends_once_for_a_session():
    links, _ = store()
    token = links.mint()

    cookie = links.spend(token)

    assert cookie
    assert links.validate(cookie)


def test_the_cookie_is_not_the_token():
    """They travel through different channels — the token through the address bar
    and the OS's open-URL handoff, the cookie only over the loopback connection —
    so a token that leaked must not be a session."""
    links, _ = store()
    token = links.mint()

    cookie = links.spend(token)

    assert cookie != token
    assert not links.validate(token)


def test_two_links_issue_two_different_sessions():
    links, _ = store()

    first = links.spend(links.mint())
    second = links.spend(links.mint())

    assert first != second
    assert links.validate(first)
    assert links.validate(second)


def test_a_respend_inside_the_grace_window_reissues_the_same_session():
    """A link scanner, a terminal's URL preview or a double-click spends the
    token before the operator's real navigation lands. Answering with the session
    it already issued grants nothing new and saves the operator a dead-link page
    for a link they just made."""
    links, clock = store()
    token = links.mint()

    first = links.spend(token)
    clock.advance(GRACE_S - 1)
    again = links.spend(token)

    assert again == first


def test_a_respend_after_the_grace_window_is_refused():
    links, clock = store()
    token = links.mint()
    cookie = links.spend(token)

    clock.advance(GRACE_S + 1)

    assert links.spend(token) is None
    # The session it issued is untouched: the LINK is used up, the login is not.
    assert links.validate(cookie)


def test_an_unspent_link_expires():
    """Single use only bounds a token that gets USED. One that never is — no
    default browser, a launch that failed, a cancelled click — would otherwise be
    a live credential with no expiry."""
    links, clock = store()
    token = links.mint()

    clock.advance(TOKEN_TTL_S + 1)

    assert links.spend(token) is None


def test_an_unspent_link_expires_even_when_the_sweep_never_runs():
    """`test_an_unspent_link_expires` passes on the sweep alone, so it would not
    notice `spend` losing its own TTL check — and then throttling the sweep, or
    moving it to `mint`, would turn every never-spent link into a permanent
    credential. This holds `spend` to the rule directly."""
    links, clock = store()
    token = links.mint()
    links._sweep = lambda: None  # type: ignore[method-assign]

    clock.advance(TOKEN_TTL_S + 1)

    assert links.spend(token) is None


def test_an_unknown_token_is_refused():
    links, _ = store()
    links.mint()

    assert links.spend("nope") is None
    assert links.spend("") is None


def test_validate_refuses_a_cookie_this_store_never_issued():
    links, _ = store()
    other, _ = store()
    borrowed = other.spend(other.mint())

    assert not links.validate(borrowed)
    assert not links.validate(None)
    assert not links.validate("")


def test_non_ascii_credentials_compare_without_crashing():
    """The #194 shape, at the one new credential-comparison site: every compare
    here goes through `auth.utf8_compare_digest`, so a non-ASCII value answers
    False instead of raising `TypeError` out of `hmac.compare_digest`."""
    links, _ = store()
    # A session has to EXIST for `validate` to compare against anything: with none
    # issued it answers False from an empty scan and never reaches compare_digest,
    # so the #194 regression this pins would pass unnoticed.
    assert links.spend(links.mint())

    assert links.spend("kaffekopp-æøå") is None
    assert not links.validate("kaffekopp-æøå")


def test_expired_and_used_up_links_are_not_retained():
    """Swept on touch, so a long-running Recorder does not accumulate one entry
    per link the operator ever asked for."""
    links, clock = store()
    for _ in range(5):
        links.mint()
    spent = links.mint()
    links.spend(spent)

    clock.advance(TOKEN_TTL_S + GRACE_S + 1)
    links.mint()  # any touch sweeps

    assert len(links._links) == 1


def test_a_session_nobody_uses_expires():
    """The browser hands this cookie to every server on localhost, not just this
    port's (ADR-0023). A copy taken there and put in a drawer must stop working."""
    links, clock = store()
    cookie = links.spend(links.mint())

    clock.advance(SESSION_IDLE_S + 1)

    assert not links.validate(cookie)


def test_a_session_in_use_outlives_the_idle_limit():
    """An open dashboard polls every 0.5-2 s, so a tab that is open must never be
    signed out by the idle limit, however long it stays open."""
    links, clock = store()
    cookie = links.spend(links.mint())

    for _ in range(4):
        clock.advance(SESSION_IDLE_S - 1)
        assert links.validate(cookie)


def test_a_session_in_use_still_ends_at_the_hard_limit():
    """The idle limit alone lets whoever holds a copy keep it alive forever by
    using it. The hard limit is what bounds a copy that is kept warm."""
    links, clock = store()
    cookie = links.spend(links.mint())

    elapsed = 0.0
    while elapsed + SESSION_IDLE_S / 2 <= SESSION_MAX_S:
        clock.advance(SESSION_IDLE_S / 2)
        elapsed += SESSION_IDLE_S / 2
        assert links.validate(cookie)

    clock.advance(SESSION_IDLE_S / 2)

    assert not links.validate(cookie)


def test_an_expired_session_is_not_refreshed_back_to_life():
    """A refused cookie must stay refused: marking it used on the failing check
    would make the NEXT check succeed."""
    links, clock = store()
    cookie = links.spend(links.mint())
    clock.advance(SESSION_IDLE_S + 1)

    assert not links.validate(cookie)
    assert not links.validate(cookie)


def test_expired_sessions_are_not_retained():
    """Retired as `validate` walks them, so a long-running Recorder does not keep
    one entry per sign-in forever, and the hot path does not slow down with them."""
    links, clock = store()
    for _ in range(3):
        links.spend(links.mint())
    clock.advance(SESSION_IDLE_S + 1)
    live = links.spend(links.mint())

    assert links.validate(live)

    assert len(links._sessions) == 1
