"""Tests for pipeline.ingest.extract_mentions — the @mention regex helper."""
from pipeline.ingest import extract_mentions


def test_empty_text_returns_empty_list():
    assert extract_mentions("") == []
    assert extract_mentions(None) == []


def test_simple_username():
    out = extract_mentions("hey @alice take a look")
    assert out == [{"token": "@alice", "lookup": "alice", "kind": "username"}]


def test_dotted_username():
    out = extract_mentions("ping @john.doe please")
    assert out == [{"token": "@john.doe", "lookup": "john.doe", "kind": "username"}]


def test_underscore_and_digits():
    out = extract_mentions("@user_42 and @bob2")
    assert [m["lookup"] for m in out] == ["user_42", "bob2"]


def test_multiple_mentions_order_preserved():
    out = extract_mentions("@a then @b and finally @c")
    assert [m["lookup"] for m in out] == ["a", "b", "c"]


def test_pachca_user_id_form():
    out = extract_mentions("see <@12345> for context")
    assert out == [{"token": "<@12345>", "lookup": "12345", "kind": "user_id"}]


def test_mixed_username_and_user_id():
    out = extract_mentions("hi @alice cc <@9876>")
    assert out == [
        {"token": "@alice", "lookup": "alice", "kind": "username"},
        {"token": "<@9876>", "lookup": "9876", "kind": "user_id"},
    ]


def test_skips_broadcast_sentinels():
    out = extract_mentions("@here @channel @all @everyone @real_user")
    assert [m["lookup"] for m in out] == ["real_user"]


def test_skips_email_addresses():
    out = extract_mentions("contact me at bob@example.com or ping @bob")
    assert [m["lookup"] for m in out] == ["bob"]


def test_handles_mention_at_start_and_end():
    assert extract_mentions("@start of line")[0]["lookup"] == "start"
    assert extract_mentions("end of line @end")[0]["lookup"] == "end"


def test_duplicate_mentions_returned_twice():
    out = extract_mentions("@alice please look @alice")
    assert len(out) == 2
    assert all(m["lookup"] == "alice" for m in out)


def test_pachca_user_id_inside_text():
    out = extract_mentions("...thanks <@1> and <@22>!")
    assert [m["lookup"] for m in out] == ["1", "22"]


def test_no_match_for_lone_at_sign():
    assert extract_mentions("just an @ sign") == []
    assert extract_mentions("trailing @") == []
