"""Tests for YouTrack issue-id helpers in pipeline.ingest."""
from pipeline.ingest import (
    build_youtrack_issue_regex,
    extract_urls,
    parse_channel_issue_ids,
)


_YT = {
    "base_url": "https://youtrack.jetbrains.com",
    "project_prefixes": ["PL", "DEMO", "MOBILE"],
}


# ── build_youtrack_issue_regex ────────────────────────────────────────────────

def test_build_regex_matches_known_prefix():
    pat = build_youtrack_issue_regex(["PL", "DEMO"])
    assert pat.search("see PL-15491 here").group(0) == "PL-15491"
    assert pat.search("DEMO-7 is open").group(0) == "DEMO-7"


def test_build_regex_ignores_unknown_prefix():
    pat = build_youtrack_issue_regex(["PL"])
    assert pat.search("OTHER-123 something") is None


def test_build_regex_returns_none_for_empty_prefixes():
    assert build_youtrack_issue_regex([]) is None
    assert build_youtrack_issue_regex(None) is None


# ── extract_urls ──────────────────────────────────────────────────────────────

def test_extract_urls_classifies_youtrack_link():
    text = "fix is in https://youtrack.jetbrains.com/issue/PL-21765 thanks"
    urls = extract_urls(text, _YT)
    assert urls == [{"type": "youtrack", "issue_id": "PL-21765",
                     "url": "https://youtrack.jetbrains.com/issue/PL-21765"}]


def test_extract_urls_other_link_is_generic():
    urls = extract_urls("see https://example.com/x for details", _YT)
    assert urls == [{"type": "other", "url": "https://example.com/x"}]


def test_extract_urls_mixed_links_keeps_order():
    text = "first https://example.com/a then https://youtrack.jetbrains.com/issue/PL-1 end."
    urls = extract_urls(text, _YT)
    assert urls[0]["type"] == "other"
    assert urls[1]["type"] == "youtrack" and urls[1]["issue_id"] == "PL-1"


def test_extract_urls_strips_trailing_punctuation():
    urls = extract_urls("see https://example.com/x.", _YT)
    assert urls[0]["url"] == "https://example.com/x"


def test_extract_urls_without_youtrack_cfg_returns_other():
    urls = extract_urls("https://youtrack.jetbrains.com/issue/PL-1", None)
    assert urls == [{"type": "other", "url": "https://youtrack.jetbrains.com/issue/PL-1"}]


def test_extract_urls_empty_text():
    assert extract_urls("", _YT) == []
    assert extract_urls(None, _YT) == []


def test_extract_urls_youtrack_host_but_unknown_prefix_is_other():
    cfg = {"base_url": "https://youtrack.jetbrains.com", "project_prefixes": ["PL"]}
    urls = extract_urls("https://youtrack.jetbrains.com/issue/OTHER-9", cfg)
    assert urls == [{"type": "other", "url": "https://youtrack.jetbrains.com/issue/OTHER-9"}]


# ── parse_channel_issue_ids ───────────────────────────────────────────────────

def test_parse_channel_issue_ids_extracts_single():
    assert parse_channel_issue_ids("Dev / PL-15491 mDK v.3", ["PL"]) == ["PL-15491"]


def test_parse_channel_issue_ids_extracts_multiple_unique():
    ids = parse_channel_issue_ids("PL-1 and PL-2 and PL-1 again", ["PL"])
    assert ids == ["PL-1", "PL-2"]


def test_parse_channel_issue_ids_returns_empty_without_prefixes():
    assert parse_channel_issue_ids("PL-1 something", []) == []


def test_parse_channel_issue_ids_no_match():
    assert parse_channel_issue_ids("general announcements", ["PL"]) == []
