"""Run with: uv run --with pytest -m pytest tests"""

import asyncio

from bizzybot_agent_wrapper.mentions import MentionDirectory, unescape_mention_tokens

USERS = [
    {"id": "U0C2U8R19U5", "name": "bzpm", "profile": {"display_name": ""}},
    {"id": "U0C2X80RBN2", "name": "builder", "profile": {"display_name": "Builder"}},
    {"id": "U08HUTX82MV", "name": "scottp", "profile": {"display_name": "Scott P"}},
    {"id": "UGONE", "name": "gone", "deleted": True},
    # A display name that collides with someone else's username must not win.
    {"id": "UIMPOSTOR", "name": "impostor", "profile": {"display_name": "builder"}},
]


class FakeSlack:
    def __init__(self):
        self.calls = 0

    async def users_list(self, limit=200, cursor=None):
        self.calls += 1
        return {"members": USERS, "response_metadata": {"next_cursor": ""}}


def resolve(text, slack=None):
    d = MentionDirectory(slack or FakeSlack())
    return asyncio.run(d.resolve(text))


def test_unescape_tokens():
    assert unescape_mention_tokens("&lt;@U0C2U8R19U5&gt; hi") == "<@U0C2U8R19U5> hi"
    assert unescape_mention_tokens("&lt;!here&gt; x") == "<!here> x"
    assert unescape_mention_tokens("a &lt;b&gt; c") == "a &lt;b&gt; c"


def test_handle_becomes_token():
    assert resolve("@builder build BP-68 (readme)") == "<@U0C2X80RBN2> build BP-68 (readme)"


def test_case_insensitive_and_display_name():
    assert resolve("@Builder go") == "<@U0C2X80RBN2> go"
    assert resolve("@BzPM done") == "<@U0C2U8R19U5> done"


def test_trailing_punctuation_kept():
    assert resolve("thanks @scottp.") == "thanks <@U08HUTX82MV>."
    assert resolve("(@bzpm)") == "(<@U0C2U8R19U5>)"


def test_username_beats_display_name_collision():
    assert resolve("@builder") == "<@U0C2X80RBN2>"


def test_left_alone():
    assert resolve("mail scott@builder.com") == "mail scott@builder.com"
    assert resolve("`@builder` in code") == "`@builder` in code"
    assert resolve("```\n@builder\n```") == "```\n@builder\n```"
    assert resolve("@nobody-here yo") == "@nobody-here yo"
    assert resolve("<@U0C2X80RBN2> already") == "<@U0C2X80RBN2> already"
    assert resolve("<https://x.y|@builder>") == "<https://x.y|@builder>"
    assert resolve("no at signs") == "no at signs"
    assert resolve("@gone") == "@gone"


def test_escaped_token_repaired_in_resolve():
    assert resolve("&lt;@U0C2U8R19U5&gt; BP-68 is done") == "<@U0C2U8R19U5> BP-68 is done"


def test_users_list_cached_and_miss_throttled():
    slack = FakeSlack()
    d = MentionDirectory(slack)
    asyncio.run(d.resolve("@builder"))
    asyncio.run(d.resolve("@bzpm and @builder"))
    assert slack.calls == 1
    asyncio.run(d.resolve("@unknown1 @unknown2"))
    assert slack.calls == 2  # one forced refresh for the misses, not two
