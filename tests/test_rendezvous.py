"""Тесты чистой части rendezvous: разбор файлов gist и валидность записей."""

import json

from gpu_raid.rendezvous import ENTRY_TTL_S, entry_valid, parse_gist_files

NOW = 2_000_000.0


def _entry(**over):
    base = {
        "v": 1, "name": "colab-0", "platform": "colab", "session": "abc123",
        "string": "gpuraid://tok@xxx.trycloudflare.com?name=colab-0",
        "ts": int(NOW) - 10, "state": "up",
    }
    base.update(over)
    return base


def _files(*entries, extra=None):
    files = {f"w_{e['session']}.json": {"content": json.dumps(e)} for e in entries}
    files.update(extra or {})
    return files


def test_parse_basic():
    e = _entry()
    out = parse_gist_files(_files(e))
    assert out == [e]


def test_parse_ignores_garbage():
    files = _files(
        _entry(),
        extra={
            "readme.md": {"content": "# просто файл"},
            "w_broken.json": {"content": "{не json"},
            "w_wrong_version.json": {"content": json.dumps({"v": 2, "session": "x", "string": "y"})},
            "w_no_session.json": {"content": json.dumps({"v": 1, "string": "y"})},
            "w_no_string.json": {"content": json.dumps({"v": 1, "session": "z"})},
        },
    )
    out = parse_gist_files(files)
    assert len(out) == 1
    assert out[0]["session"] == "abc123"


def test_parse_two_workers():
    out = parse_gist_files(_files(_entry(), _entry(session="def456", name="kaggle-0",
                                                   platform="kaggle")))
    assert {e["session"] for e in out} == {"abc123", "def456"}


def test_parse_empty_and_none():
    assert parse_gist_files({}) == []
    assert parse_gist_files(None) == []


def test_valid_fresh_up():
    assert entry_valid(_entry(), NOW) is True


def test_invalid_down():
    assert entry_valid(_entry(state="down"), NOW) is False


def test_invalid_stale():
    assert entry_valid(_entry(ts=int(NOW) - ENTRY_TTL_S - 1), NOW) is False
    assert entry_valid(_entry(ts=int(NOW) - ENTRY_TTL_S + 5), NOW) is True


def test_invalid_bad_ts():
    assert entry_valid(_entry(ts=0), NOW) is False
    assert entry_valid(_entry(ts="мусор"), NOW) is False
    assert entry_valid(_entry(ts=None), NOW) is False
