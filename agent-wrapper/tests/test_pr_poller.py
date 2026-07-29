"""Tests for the PR review poller's dedup and parsing contracts.

These two seams are pure by design (`parse_search_output`, `_select_new`), and
they carry the guarantees that keep this feature from posting duplicate or
spurious reviews to other people's pull requests — the one thing in this repo
that writes publicly and unattended. `_fetch` is covered too, via a fake
subprocess, because its None/[] distinction is load-bearing.

Run: `uv run --group dev pytest` from agent-wrapper/.
"""

from __future__ import annotations

import asyncio

import pytest

from bizzybot_agent_wrapper import pr_poller
from bizzybot_agent_wrapper.pr_poller import (
    FORGET_AFTER_POLLS,
    MAX_FIRES_PER_POLL,
    MIN_FORGET_AFTER_S,
    MIN_INTERVAL_S,
    PR,
    PRPoller,
    ReviewNotStarted,
    parse_search_output,
)


class _Clock:
    """Hand-cranked monotonic clock, so the forget window is tested in seconds
    without any test actually sleeping."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _pr(number: int = 1, repo: str = "acme/widgets", **kw) -> PR:
    return PR(
        url=kw.get("url", f"https://github.com/{repo}/pull/{number}"),
        title=kw.get("title", "a title"),
        number=number,
        repo=repo,
        updated_at=kw.get("updated_at", "2026-01-01T00:00:00Z"),
        is_draft=kw.get("is_draft", False),
    )


def _poller(**kw) -> PRPoller:
    async def _noop(pr: PR) -> None:
        return None

    return PRPoller(
        login=kw.get("login", "@me"),
        on_new=kw.get("on_new", _noop),
        interval_s=kw.get("interval_s", 60.0),
        clock=kw.get("clock", _Clock()),
    )


# --- parse_search_output ----------------------------------------------------


def test_prefers_name_with_owner_over_bare_name():
    """The bare `name` collides across owners, so it must never win — two repos
    called `widgets` under different orgs would share a dedup key."""
    [pr] = parse_search_output(
        [{
            "url": "https://github.com/acme/widgets/pull/7",
            "title": "change widgets",
            "number": 7,
            "repository": {"name": "widgets", "nameWithOwner": "acme/widgets"},
        }]
    )
    assert pr.repo == "acme/widgets"
    assert pr.key == "acme/widgets#7"


def test_skips_malformed_entries():
    """Malformed upstream data must neither kill the poller nor select the
    target of an unattended write using placeholder identifiers."""
    items = ["a string", None, 42, {"number": "not-a-number", "repository": "not-an-object"}]
    assert parse_search_output(items) == []


@pytest.mark.parametrize(
    "item",
    [
        {"url": "", "title": "t", "number": 1, "repository": {"nameWithOwner": "acme/w"}},
        {"url": "https://github.com/acme/w/pull/2", "title": "t", "number": 1,
         "repository": {"nameWithOwner": "acme/w"}},
        {"url": "https://github.com/acme/w/pull/1", "title": "t", "number": 1,
         "repository": {"name": "w"}},
        {"url": "https://github.com/acme/w/pull/1", "title": None, "number": 1,
         "repository": {"nameWithOwner": "acme/w"}},
    ],
)
def test_rejects_inconsistent_write_targets(item):
    assert parse_search_output([item]) == []


# --- _select_new: the dedup / forget contract -------------------------------


def test_new_pr_is_returned_once_then_deduped():
    p = _poller()
    pr = _pr()
    assert [x.key for x in p._select_new([pr])] == [pr.key]
    p._mark_seen(pr)
    assert p._select_new([pr]) == []  # still pending, already reviewed


def test_search_index_flicker_does_not_refire():
    """GitHub's search index is eventually consistent: a still-pending PR can
    drop out of one poll and come back. Re-firing would post a second public
    review that can't be retracted."""
    clock = _Clock()
    p = _poller(clock=clock)
    pr = _pr()
    p._mark_seen(pr)
    for _ in range(20):  # absent across many polls, but well inside the window
        clock.advance(10)
        assert p._select_new([]) == []
    assert p._select_new([pr]) == []  # reappears — not re-reviewed


def test_key_survives_right_up_to_the_window_then_is_forgotten():
    clock = _Clock()
    p = _poller(clock=clock)
    pr = _pr()
    p._mark_seen(pr)

    clock.advance(p._forget_after_s)  # exactly at the boundary — still held
    p._select_new([])
    assert pr.key in p._seen

    clock.advance(1)
    p._select_new([])
    assert pr.key not in p._seen


def test_forget_window_is_wall_clock_not_poll_count():
    """The whole point of the change: a poller at the 10s floor must get the
    same protection as one at the 60s default. Under poll-counting this key
    would have been forgotten after 4 polls (40s)."""
    clock = _Clock()
    p = _poller(interval_s=MIN_INTERVAL_S, clock=clock)
    pr = _pr()
    p._mark_seen(pr)
    assert p._forget_after_s == MIN_FORGET_AFTER_S  # floor, not 4 x 10s

    for _ in range(10):  # 100s of absence — 10 polls, still protected
        clock.advance(MIN_INTERVAL_S)
        p._select_new([])
    assert pr.key in p._seen
    assert p._select_new([pr]) == []  # a stale index read does not re-review


def test_long_interval_scales_the_window_up():
    """No ceiling: a long interval must not let a key expire between two polls,
    which would reopen the duplicate-review hole."""
    p = _poller(interval_s=600.0)
    assert p._forget_after_s == FORGET_AFTER_POLLS * 600.0
    assert p._forget_after_s > 600.0  # survives at least one whole interval


def test_genuine_rerequest_fires_again_once_forgotten():
    clock = _Clock()
    p = _poller(clock=clock)
    pr = _pr()
    p._mark_seen(pr)
    clock.advance(p._forget_after_s + 1)
    p._select_new([])
    assert [x.key for x in p._select_new([pr])] == [pr.key]


def test_presence_refreshes_the_timestamp():
    """A PR that stays pending for hours must never age out while it is still
    being returned by the search."""
    clock = _Clock()
    p = _poller(clock=clock)
    pr = _pr()
    p._mark_seen(pr)
    for _ in range(50):
        clock.advance(p._forget_after_s * 0.9)
        assert p._select_new([pr]) == []  # present each time -> never forgotten
    assert pr.key in p._seen


def test_duplicate_search_entries_are_selected_only_once():
    p = _poller()
    pr = _pr()
    assert p._select_new([pr, pr]) == [pr]


def test_select_new_does_not_claim_the_batch():
    """The whole batch must not be marked seen up front. If it were, a shutdown
    part-way through the queue would record PRs as handled that were never
    reviewed — and a restart re-seeds pending requests as seen, so they'd never
    be reviewed at all."""
    p = _poller()
    prs = [_pr(1), _pr(2), _pr(3)]
    new = p._select_new(prs)
    assert len(new) == 3
    assert p._seen == {}  # nothing claimed yet

    p._mark_seen(new[0])  # simulate: first one fires, then we're interrupted
    still_new = [x.key for x in p._select_new(prs)]
    assert still_new == ["acme/widgets#2", "acme/widgets#3"]


# --- interval floor ---------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 1.0, -5.0, 0.0])
def test_unusable_intervals_are_floored(bad):
    """NaN in particular: every comparison against it is False, so a plain
    `x < floor` check lets it through and `asyncio.sleep(nan)` then raises on
    every pass of the poll loop, busy-spinning."""
    assert _poller(interval_s=bad)._interval_s == MIN_INTERVAL_S


def test_reasonable_interval_is_kept():
    assert _poller(interval_s=120.0)._interval_s == 120.0


# --- _fetch: the None vs [] distinction -------------------------------------


class _FakeProc:
    def __init__(self, stdout: bytes, returncode: int = 0):
        self._stdout, self.returncode = stdout, returncode

    async def communicate(self):
        return self._stdout, b""

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


def _fetch_with(monkeypatch, stdout: bytes, returncode: int = 0):
    async def fake_exec(*a, **kw):
        return _FakeProc(stdout, returncode)

    monkeypatch.setattr(pr_poller.asyncio, "create_subprocess_exec", fake_exec)
    return asyncio.run(_poller()._fetch())


def test_empty_queue_is_a_list_not_none(monkeypatch):
    assert _fetch_with(monkeypatch, b"[]") == []


def test_nonzero_exit_is_none(monkeypatch):
    assert _fetch_with(monkeypatch, b"", returncode=1) is None


def test_invalid_json_is_none(monkeypatch):
    assert _fetch_with(monkeypatch, b"not json at all") is None


def test_json_object_is_none_not_empty(monkeypatch):
    """`gh` exiting 0 with an API error object must read as a failed poll. If it
    read as an empty queue during seeding, the next successful poll would treat
    every pending request as new and review all of them at once."""
    assert _fetch_with(monkeypatch, b'{"message":"API rate limit exceeded"}') is None


def test_json_null_is_none_not_empty(monkeypatch):
    assert _fetch_with(monkeypatch, b"null") is None


def test_successful_fetch_parses(monkeypatch):
    payload = b'[{"url":"https://github.com/acme/w/pull/3","title":"t","number":3,"repository":{"nameWithOwner":"acme/w"}}]'
    out = _fetch_with(monkeypatch, payload)
    assert [p.key for p in out] == ["acme/w#3"]


# --- dispatch failure semantics ---------------------------------------------


def test_definitely_not_started_dispatch_is_retried():
    calls = 0
    pr = _pr()

    async def on_new(item: PR) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ReviewNotStarted("Slack announcement failed")
        raise asyncio.CancelledError

    async def exercise() -> None:
        p = PRPoller(login="@me", on_new=on_new, interval_s=60)
        responses = iter([[], [pr], [pr]])

        async def fetch():
            return next(responses)

        async def no_sleep(_seconds):
            return None

        p._fetch = fetch
        original_sleep = pr_poller.asyncio.sleep
        pr_poller.asyncio.sleep = no_sleep
        try:
            with pytest.raises(asyncio.CancelledError):
                await p._loop()
        finally:
            pr_poller.asyncio.sleep = original_sleep

    asyncio.run(exercise())
    assert calls == 2


# --- fire cap ---------------------------------------------------------------


def test_overflow_is_deferred_not_dropped():
    """A bulk reviewer-add shouldn't produce an unbounded burst of unattended
    writes; the excess must survive to the next poll rather than vanish."""
    fired: list[str] = []

    async def record(pr: PR) -> None:
        fired.append(pr.key)

    p = _poller(on_new=record)
    prs = [_pr(i) for i in range(1, MAX_FIRES_PER_POLL + 3)]

    new = p._select_new(prs)
    assert len(new) == len(prs)
    capped = new[:MAX_FIRES_PER_POLL]
    for pr in capped:
        p._mark_seen(pr)

    # The deferred remainder was never claimed, so the next poll still sees it.
    leftover = [x.key for x in p._select_new(prs)]
    assert leftover == [pr.key for pr in new[MAX_FIRES_PER_POLL:]]
