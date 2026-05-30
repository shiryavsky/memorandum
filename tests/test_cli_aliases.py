"""Tests for cli/aliases.py — the append-only stub generator."""
import io
import json
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

import pytest

from cli.aliases import (
    _build_known_alias_index,
    _candidate_entries,
    _stub_yaml,
    _candidates_json,
    _append_in_place,
    _format_seen,
    refresh,
)


def _insert_messages(db, senders_counts: list, source: str = "src"):
    """Insert N messages per (sender, count) into the tmp_db fixture."""
    for i, (sender, count) in enumerate(senders_counts):
        for j in range(count):
            db.insert({
                "id": f"{source}:{sender}:{i}:{j}",
                "source": source,
                "channel_id": "ch1",
                "sender": sender,
                "sender_id": sender,
                "timestamp": "2026-05-27T10:00:00+00:00",
                "text": "hi",
                "thread_id": None,
                "reply_to_id": None,
                "tags": [],
                "raw": {},
            })


# ── _build_known_alias_index ──────────────────────────────────────────────────

def test_known_index_includes_canonical_and_aliases_lowercased():
    user_aliases = [
        {"canonical_name": "Jane Smith", "aliases": ["jane", "JSMITH"]},
        {"canonical_name": "bob", "aliases": []},
    ]
    idx = _build_known_alias_index(user_aliases)
    assert "jane smith" in idx
    assert "jane" in idx
    assert "jsmith" in idx
    assert "bob" in idx


def test_known_index_tolerates_missing_or_empty_fields():
    user_aliases = [
        {"canonical_name": ""},                # empty canonical, ignored
        {"canonical_name": "Alice"},           # no aliases list — still indexed
        {"aliases": ["x", ""]},                # no canonical, empty alias dropped
        "not a dict",                          # garbage, ignored
    ]
    idx = _build_known_alias_index(user_aliases)
    assert idx == {"alice", "x"}


def test_known_index_handles_none_input():
    assert _build_known_alias_index(None) == set()


def test_known_index_includes_my_aliases():
    idx = _build_known_alias_index(
        user_aliases=[],
        my_aliases=["John Doe", "john.doe", "johnd"],
    )
    assert "john doe" in idx
    assert "john.doe" in idx
    assert "johnd" in idx


def test_candidates_exclude_my_aliases(tmp_db):
    _insert_messages(tmp_db, [("john.doe", 460), ("alice", 3)])
    cands = _candidate_entries(
        tmp_db,
        user_aliases=[],
        my_aliases=["John Doe", "john.doe", "johnd"],
    )
    names = [c["canonical_name"] for c in cands]
    assert "john.doe" not in names
    assert names == ["alice"]


# ── _candidate_entries (the diff) ─────────────────────────────────────────────

def test_candidates_exclude_existing_aliases(tmp_db):
    _insert_messages(tmp_db, [("alice", 3), ("bob", 1), ("charlie", 2)])
    user_aliases = [{"canonical_name": "Bob", "aliases": ["bob", "bwilson"]}]

    cands = _candidate_entries(tmp_db, user_aliases)
    names = [c["canonical_name"] for c in cands]

    assert "bob" not in names              # already covered
    assert "alice" in names and "charlie" in names


def test_candidates_ordered_by_message_count_desc(tmp_db):
    _insert_messages(tmp_db, [("low", 1), ("high", 10), ("mid", 4)])

    cands = _candidate_entries(tmp_db, [])
    assert [c["canonical_name"] for c in cands] == ["high", "mid", "low"]
    assert [c["count"] for c in cands] == [10, 4, 1]


def test_candidates_case_insensitive_match(tmp_db):
    _insert_messages(tmp_db, [("Alice", 1)])
    user_aliases = [{"canonical_name": "ALICE", "aliases": []}]

    assert _candidate_entries(tmp_db, user_aliases) == []


def test_candidates_drop_empty_senders(tmp_db):
    _insert_messages(tmp_db, [("alice", 1)])
    # Sneak in a message with empty sender directly
    tmp_db.conn.execute("""
        INSERT INTO messages (id, source, channel_id, sender, timestamp, text, tags, raw)
        VALUES ('src:empty', 'src', 'ch1', '', '2026-05-27T10:00:00+00:00', 'hi', '[]', '{}')
    """)
    tmp_db.conn.commit()

    names = [c["canonical_name"] for c in _candidate_entries(tmp_db, [])]
    assert names == ["alice"]


# ── _stub_yaml ────────────────────────────────────────────────────────────────

def test_stub_yaml_renders_each_candidate_with_hint_comments():
    out = _stub_yaml([{"canonical_name": "alice", "aliases": ["alice"], "count": 7,
                       "sources": {"work_telegram": 7}}])
    assert "alice" in out
    assert "seen 7 times in work_telegram" in out
    assert "# internal: true" in out
    assert "# role:" in out


# ── _format_seen (source breakdown in comments) ───────────────────────────────

def test_format_seen_single_source_names_it():
    out = _format_seen({"count": 460, "sources": {"work_telegram": 460}})
    assert out == "seen 460 times in work_telegram"


def test_format_seen_multi_source_lists_breakdown():
    out = _format_seen({"count": 100, "sources": {"a": 30, "b": 70}})
    assert out == "seen 100 times (b: 70, a: 30)"


def test_format_seen_no_sources_falls_back():
    assert _format_seen({"count": 5, "sources": {}}) == "seen 5 times"


def test_candidate_carries_per_source_breakdown(tmp_db):
    _insert_messages(tmp_db, [("dual", 3)], source="work_telegram")
    _insert_messages(tmp_db, [("dual", 2)], source="company_mattermost")
    cands = _candidate_entries(tmp_db, [], [])
    assert cands[0]["canonical_name"] == "dual"
    assert cands[0]["count"] == 5
    assert cands[0]["sources"] == {"work_telegram": 3, "company_mattermost": 2}


def test_stub_yaml_handles_empty_list():
    out = _stub_yaml([])
    assert "No new senders" in out


def test_stub_yaml_escapes_embedded_quotes():
    out = _stub_yaml([{"canonical_name": 'evil"name', "aliases": ['evil"name'], "count": 1}])
    assert '\\"' in out  # quotes escaped in the YAML stub


# ── _candidates_json ──────────────────────────────────────────────────────────

def test_candidates_json_shape():
    out = _candidates_json([{"canonical_name": "x", "aliases": ["x"], "count": 3,
                             "sources": {"src": 3}}])
    parsed = json.loads(out)
    assert parsed == [{"canonical_name": "x", "aliases": ["x"], "count": 3,
                       "sources": {"src": 3}}]


# ── _append_in_place (ruamel round-trip preserves comments) ───────────────────

_FIXTURE_CONFIG = """\
# Top-level header comment.
sqlite_path: "data/messages.db"

my_aliases:
  - "john"

# This block lists known users. Keep entries alphabetical.
user_aliases:
  - canonical_name: "Jane Smith"
    aliases: ["jane", "jsmith"]
    internal: true   # explicit
"""


def test_append_in_place_preserves_comments_and_appends(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_FIXTURE_CONFIG)

    n = _append_in_place(str(cfg), [
        {"canonical_name": "alice", "aliases": ["alice"], "count": 5},
        {"canonical_name": "bob", "aliases": ["bob"], "count": 2},
    ])
    assert n == 2

    out = cfg.read_text()
    # Comments survive
    assert "# Top-level header comment." in out
    assert "# This block lists known users. Keep entries alphabetical." in out
    assert "# explicit" in out
    # Original entry intact
    assert "Jane Smith" in out
    # New entries appended
    assert "alice" in out and "bob" in out
    # Inline note added on the new entries' canonical_name line
    assert "added by `memorandum aliases refresh`" in out


def test_append_in_place_creates_user_aliases_when_missing(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("# only top-level\nsqlite_path: 'x'\n")

    _append_in_place(str(cfg), [{"canonical_name": "alice", "aliases": ["alice"], "count": 1}])
    out = cfg.read_text()
    assert "user_aliases:" in out
    assert "alice" in out


# ── refresh() entry point — exit codes & dispatch ─────────────────────────────

@patch("cli.aliases.load_config")
def test_refresh_exits_2_on_missing_config(mock_load):
    mock_load.side_effect = FileNotFoundError("nope")
    with pytest.raises(SystemExit) as exc:
        refresh(config_path="nope.yaml")
    assert exc.value.code == 2


@patch("cli.aliases.load_config")
@patch("cli.aliases.Database")
def test_refresh_exits_2_on_db_failure(mock_db_cls, mock_load):
    mock_load.return_value = {"sqlite_path": "/missing.db"}
    mock_db_cls.side_effect = OSError("disk gone")
    with pytest.raises(SystemExit) as exc:
        refresh(config_path="cfg.yaml")
    assert exc.value.code == 2


def test_refresh_stdout_emits_yaml_block(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    db_path = tmp_path / "msgs.db"
    cfg.write_text(f'sqlite_path: "{db_path}"\nuser_aliases: []\n')

    from storage.db import Database
    db = Database(str(db_path))
    _insert_messages(db, [("alice", 3)])
    db.close()

    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        refresh(config_path=str(cfg))
    assert exc.value.code == 0
    text = buf.getvalue()
    assert "alice" in text and "seen 3 times" in text


def test_refresh_json_mode(tmp_path):
    cfg = tmp_path / "config.yaml"
    db_path = tmp_path / "msgs.db"
    cfg.write_text(f'sqlite_path: "{db_path}"\nuser_aliases: []\n')

    from storage.db import Database
    db = Database(str(db_path))
    _insert_messages(db, [("alice", 2)])
    db.close()

    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        refresh(config_path=str(cfg), as_json=True)
    assert exc.value.code == 0
    parsed = json.loads(buf.getvalue())
    assert parsed == [{"canonical_name": "alice", "aliases": ["alice"], "count": 2,
                       "sources": {"src": 2}}]


def test_refresh_in_place_appends_to_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    db_path = tmp_path / "msgs.db"
    cfg.write_text(
        f'# header\nsqlite_path: "{db_path}"\nuser_aliases:\n'
        '  - canonical_name: "Jane"\n    aliases: [jane]\n'
    )

    from storage.db import Database
    db = Database(str(db_path))
    _insert_messages(db, [("alice", 1)])
    db.close()

    err = io.StringIO()
    with redirect_stderr(err), pytest.raises(SystemExit) as exc:
        refresh(config_path=str(cfg), in_place=True)
    assert exc.value.code == 0
    assert "Appended 1 stub" in err.getvalue()

    out = cfg.read_text()
    assert "# header" in out         # comment survived
    assert "Jane" in out             # existing entry survived
    assert "alice" in out            # new entry appended
