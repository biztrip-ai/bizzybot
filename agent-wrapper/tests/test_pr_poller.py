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
    FORGET_AFTER_MISSES,
    MAX_FIRES_PER_POLL,
    MIN_INTERVAL_S,
    PR,
    PRPoller,
    parse_search_output,
)


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

    return PRPoller(login=kw.get("login", "@me"), on_new=kw.get("on_new", _noop),
                    interval_s=kw.get("interval_s", 60.0))


# --- parse_search_output ----------------------------------------------------


def test_prefers_name_with_owner_over_bare_name():
    """The bare `name` collides across owners, so it must never win — two repos
    called `widgets` under different orgs would share a dedup key."""
    [pr] = parse_search_output(
        [{"number": 7, "repository": {"name": "widgets", "nameWithOwner": "acme/widgets"}}]
    )
    assert pr.repo == "acme/widgets"
    assert pr.key == "acme/widgets#7"


def test_survives_malformed_entries():
    """An exception escaping the parser during seeding would kill the poll task
    outright and disable the feature until restart."""
    items = ["a string", None, 42, {"number": "not-a-number", "repository": "not-an-object"}]
    out = parse_search_output(items)
    assert len(out) == 1  # only the dict survives
    assert out[0].number == 0
    assert out[0].repo == "?"


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
    p = _poller()
    pr = _pr()
    p._mark_seen(pr)
    for _ in range(FORGET_AFTER_MISSES - 1):  # absent, but not long enough
        assert p._select_new([]) == []
    assert p._select_new([pr]) == []  # reappears — not re-reviewed


def test_key_forgotten_after_exactly_forget_after_misses():
    """The constant means what it says: dropped on the Nth absence, not N+1."""
    p = _poller()
    pr = _pr()
    p._mark_seen(pr)
    for _ in range(FORGET_AFTER_MISSES - 1):
        p._select_new([])
        assert pr.key in p._seen
    p._select_new([])
    assert pr.key not in p._seen


def test_genuine_rerequest_fires_again_once_forgotten():
    p = _poller()
    pr = _pr()
    p._mark_seen(pr)
    for _ in range(FORGET_AFTER_MISSES):
        p._select_new([])
    assert [x.key for x in p._select_new([pr])] == [pr.key]


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
    payload = b'[{"url":"u","title":"t","number":3,"repository":{"nameWithOwner":"acme/w"}}]'
    out = _fetch_with(monkeypatch, payload)
    assert [p.key for p in out] == ["acme/w#3"]


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
