"""The login-link store: single use, a grace window, and an expiry.

Driven directly, with no HTTP and no app — the point of keeping the state
machine in its own module (ADR-0023). The clock is injected, so nothing here
sleeps.
"""

from __future__ import annotations

from tapscribe.login_links import GRACE_S, TOKEN_TTL_S, LoginLinks


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
    links.mint()

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
