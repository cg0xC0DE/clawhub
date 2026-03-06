"""Tests for eunuch.py — message pool, parsing, whisper anonymity, submit/query."""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import eunuch


# ── _content_hash ─────────────────────────────────────────────


class TestContentHash:
    def test_returns_8_char_hex(self):
        h = eunuch._content_hash("test")
        assert len(h) == 8
        assert all(c in "0123456789abcdef" for c in h)

    def test_deterministic(self):
        assert eunuch._content_hash("abc") == eunuch._content_hash("abc")

    def test_different_inputs_different_hashes(self):
        assert eunuch._content_hash("a") != eunuch._content_hash("b")


# ── MessagePool ───────────────────────────────────────────────


class TestMessagePool:
    def test_add_and_query(self, tmp_path):
        pool = eunuch.MessagePool(tmp_path / "pool.jsonl")
        msg = {"id": "m1", "type": "group", "timestamp": "2025-01-01T00:00:00Z",
               "content": "hello", "target": ""}
        assert pool.add(msg) is True
        assert pool.size == 1

    def test_dedup_by_id(self, tmp_path):
        pool = eunuch.MessagePool(tmp_path / "pool.jsonl")
        msg = {"id": "m1", "type": "group", "timestamp": "2025-01-01T00:00:00Z"}
        pool.add(msg)
        assert pool.add(msg) is False  # duplicate
        assert pool.size == 1

    def test_query_by_type(self, tmp_path):
        pool = eunuch.MessagePool(tmp_path / "pool.jsonl")
        pool.add({"id": "m1", "type": "group", "timestamp": "2025-01-01T00:00:00Z"})
        pool.add({"id": "m2", "type": "chat", "timestamp": "2025-01-01T00:01:00Z", "target": "bot001"})
        pool.add({"id": "m3", "type": "whisper", "timestamp": "2025-01-01T00:02:00Z", "target": "bot002"})

        groups = pool.query(types=["group"])
        assert len(groups) == 1
        assert groups[0]["id"] == "m1"

    def test_query_by_target(self, tmp_path):
        pool = eunuch.MessagePool(tmp_path / "pool.jsonl")
        pool.add({"id": "m1", "type": "whisper", "timestamp": "2025-01-01T00:00:00Z", "target": "bot001"})
        pool.add({"id": "m2", "type": "whisper", "timestamp": "2025-01-01T00:01:00Z", "target": "bot002"})

        result = pool.query(types=["whisper"], target="bot001")
        assert len(result) == 1
        assert result[0]["target"] == "bot001"

    def test_query_since(self, tmp_path):
        pool = eunuch.MessagePool(tmp_path / "pool.jsonl")
        pool.add({"id": "m1", "type": "group", "timestamp": "2025-01-01T00:00:00Z"})
        pool.add({"id": "m2", "type": "group", "timestamp": "2025-01-02T00:00:00Z"})

        result = pool.query(since="2025-01-01T12:00:00Z")
        assert len(result) == 1
        assert result[0]["id"] == "m2"

    def test_query_limit(self, tmp_path):
        pool = eunuch.MessagePool(tmp_path / "pool.jsonl")
        for i in range(10):
            pool.add({"id": f"m{i}", "type": "group", "timestamp": f"2025-01-01T00:{i:02d}:00Z"})

        result = pool.query(limit=3)
        assert len(result) == 3

    def test_group_messages_visible_to_all(self, tmp_path):
        pool = eunuch.MessagePool(tmp_path / "pool.jsonl")
        pool.add({"id": "m1", "type": "group", "timestamp": "2025-01-01T00:00:00Z", "target": ""})

        result = pool.query(types=["group"], target="anyone")
        assert len(result) == 1  # group messages ignore target filter

    def test_persist_and_reload(self, tmp_path):
        pool_file = tmp_path / "pool.jsonl"
        pool = eunuch.MessagePool(pool_file)
        pool.add({"id": "m1", "type": "group", "timestamp": "2025-01-01T00:00:00Z",
                  "content": "hello"})
        pool.persist()

        pool2 = eunuch.MessagePool(pool_file)
        assert pool2.size == 1
        msgs = pool2.query()
        assert msgs[0]["content"] == "hello"

    def test_trim_max_size(self, tmp_path):
        pool = eunuch.MessagePool(tmp_path / "pool.jsonl", max_size=5)
        for i in range(10):
            pool.add({"id": f"m{i}", "type": "group", "timestamp": f"2025-01-01T00:{i:02d}:00Z"})
        assert pool.size == 5

    def test_clear(self, tmp_path):
        pool_file = tmp_path / "pool.jsonl"
        pool = eunuch.MessagePool(pool_file)
        pool.add({"id": "m1", "type": "group", "timestamp": "2025-01-01T00:00:00Z"})
        with patch.object(eunuch, "reset_read_progress"):
            pool.clear(reset_collector=True)
        assert pool.size == 0

    def test_count_by_type(self, tmp_path):
        pool = eunuch.MessagePool(tmp_path / "pool.jsonl")
        pool.add({"id": "m1", "type": "group", "timestamp": "2025-01-01T00:00:00Z"})
        pool.add({"id": "m2", "type": "group", "timestamp": "2025-01-01T00:01:00Z"})
        pool.add({"id": "m3", "type": "chat", "timestamp": "2025-01-01T00:02:00Z", "target": ""})
        counts = pool.count_by_type()
        assert counts["group"] == 2
        assert counts["chat"] == 1

    def test_get_all(self, tmp_path):
        pool = eunuch.MessagePool(tmp_path / "pool.jsonl")
        for i in range(5):
            pool.add({"id": f"m{i}", "type": "group", "timestamp": f"2025-01-01T00:{i:02d}:00Z"})
        all_msgs = pool.get_all()
        assert len(all_msgs) == 5

    def test_get_all_respects_limit(self, tmp_path):
        pool = eunuch.MessagePool(tmp_path / "pool.jsonl")
        for i in range(10):
            pool.add({"id": f"m{i}", "type": "group", "timestamp": f"2025-01-01T00:{i:02d}:00Z"})
        result = pool.get_all(limit=3)
        assert len(result) == 3


# ── _parse_user_message ───────────────────────────────────────


class TestParseUserMessage:
    def test_skips_heartbeat(self):
        text = "HEARTBEAT prompt\nCurrent time: 2025-01-01"
        assert eunuch._parse_user_message(text) is None

    def test_parses_plain_text(self):
        result = eunuch._parse_user_message("这是一条普通消息")
        assert result is not None
        assert result["text"] == "这是一条普通消息"

    def test_parses_metadata_blocks(self):
        text = (
            'Conversation info (untrusted metadata): ```json\n'
            '{"conversation_label": "群聊-100999"}\n'
            '```\n'
            'Sender (untrusted metadata): ```json\n'
            '{"name": "Alice", "label": "Alice"}\n'
            '```\n'
            '实际消息内容'
        )
        result = eunuch._parse_user_message(text)
        assert result is not None
        assert result["text"] == "实际消息内容"
        assert result["conv_info"]["conversation_label"] == "群聊-100999"
        assert result["sender_info"]["name"] == "Alice"

    def test_returns_none_for_empty_after_strip(self):
        text = (
            'Sender (untrusted metadata): ```json\n'
            '{"name": "Bob"}\n'
            '```\n'
        )
        result = eunuch._parse_user_message(text)
        assert result is None


# ── _parse_assistant_text ─────────────────────────────────────


class TestParseAssistantText:
    def test_extracts_text_blocks(self):
        content = [
            {"type": "text", "text": "Hello world"},
            {"type": "tool_use", "name": "read"},
        ]
        assert eunuch._parse_assistant_text(content) == "Hello world"

    def test_strips_final_tags(self):
        content = [{"type": "text", "text": "<final>Reply here</final>"}]
        assert eunuch._parse_assistant_text(content) == "Reply here"

    def test_joins_multiple_text_blocks(self):
        content = [
            {"type": "text", "text": "Part 1"},
            {"type": "text", "text": "Part 2"},
        ]
        result = eunuch._parse_assistant_text(content)
        assert "Part 1" in result
        assert "Part 2" in result

    def test_empty_content(self):
        assert eunuch._parse_assistant_text([]) == ""

    def test_skips_empty_text(self):
        content = [{"type": "text", "text": "  "}, {"type": "text", "text": "Real"}]
        assert eunuch._parse_assistant_text(content) == "Real"


# ── _resolve_sender ───────────────────────────────────────────


class TestResolveSender:
    def test_owner_by_alias(self, sample_palace):
        sid, sdisp = eunuch._resolve_sender({"name": "用户"}, sample_palace)
        assert sid == "owner"
        assert sdisp == "测试用户"

    def test_owner_by_empty_name(self, sample_palace):
        sid, sdisp = eunuch._resolve_sender({}, sample_palace)
        assert sid == "owner"

    def test_owner_by_anonymous(self, sample_palace):
        sid, sdisp = eunuch._resolve_sender({"name": "GroupAnonymousBot"}, sample_palace)
        assert sid == "owner"

    def test_participant_by_name(self, sample_palace):
        sid, sdisp = eunuch._resolve_sender({"name": "孙子"}, sample_palace)
        assert sid == "bot001"
        assert sdisp == "孙子"

    def test_participant_by_id(self, sample_palace):
        sid, sdisp = eunuch._resolve_sender({"name": "bot002"}, sample_palace)
        assert sid == "bot002"

    def test_unknown_sender(self, sample_palace):
        sid, sdisp = eunuch._resolve_sender({"name": "Stranger"}, sample_palace)
        assert sid == "unknown"
        assert sdisp == "Stranger"


# ── _check_whisper_anonymity ──────────────────────────────────


class TestCheckWhisperAnonymity:
    def test_clean_whisper_passes(self, sample_palace):
        result = eunuch._check_whisper_anonymity(
            "听说有人在打算谋反", "bot001", sample_palace
        )
        assert result is None

    def test_leaks_name(self, sample_palace):
        result = eunuch._check_whisper_anonymity(
            "我是孙子，告诉你一个秘密", "bot001", sample_palace
        )
        assert result is not None
        assert "孙子" in result

    def test_leaks_id(self, sample_palace):
        result = eunuch._check_whisper_anonymity(
            "这是bot001说的话", "bot001", sample_palace
        )
        assert result is not None

    def test_leaks_alias(self, sample_palace):
        result = eunuch._check_whisper_anonymity(
            "兵圣说的话", "bot001", sample_palace
        )
        assert result is not None

    def test_self_referential_pronoun(self, sample_palace):
        result = eunuch._check_whisper_anonymity(
            "妾身知道一些事情", "bot001", sample_palace
        )
        assert result is not None
        assert "妾身" in result


# ── _format_briefing ──────────────────────────────────────────


class TestFormatBriefing:
    def test_groups_by_type(self):
        messages = [
            {"type": "group", "timestamp": "2025-01-01T00:00:00Z",
             "sender_display": "Alice", "content": "群消息"},
            {"type": "whisper", "timestamp": "2025-01-01T00:01:00Z",
             "sender_display": "匿名", "content": "密语"},
            {"type": "decree", "timestamp": "2025-01-01T00:02:00Z",
             "sender_display": "系统", "content": "通知"},
        ]
        with patch.object(eunuch, "_load_palace", return_value={}):
            result = eunuch._format_briefing(messages, "bot001")
        assert result["message_count"] == 3
        assert result["sections"]["群组讨论"] == 1
        assert result["sections"]["匿名消息"] == 1
        assert result["sections"]["系统通知"] == 1

    def test_empty_messages(self):
        with patch.object(eunuch, "_load_palace", return_value={}):
            result = eunuch._format_briefing([], "bot001")
        assert result["message_count"] == 0
        assert "暂无" in result["text"]

    def test_truncates_long_content(self):
        messages = [
            {"type": "group", "timestamp": "2025-01-01T00:00:00Z",
             "sender_display": "Alice", "content": "x" * 500},
        ]
        with patch.object(eunuch, "_load_palace", return_value={}):
            result = eunuch._format_briefing(messages, "bot001")
        assert "..." in result["text"]


# ── log_action ────────────────────────────────────────────────


class TestLogAction:
    def test_adds_eunuch_log_entry(self, tmp_path):
        pool = eunuch.MessagePool(tmp_path / "pool.jsonl")
        eunuch.log_action(pool, "测试动作", "详细信息", "bot001")
        msgs = pool.get_all()
        assert len(msgs) == 1
        assert msgs[0]["type"] == "eunuch_log"
        assert "测试动作" in msgs[0]["content"]


# ── _detect_session_key ───────────────────────────────────────


class TestDetectSessionKey:
    def _write_jsonl(self, path, entries):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def test_detects_group_session(self, tmp_path):
        eunuch._session_key_cache.clear()
        jsonl = tmp_path / "session.jsonl"
        self._write_jsonl(jsonl, [
            {"type": "message", "message": {
                "role": "user",
                "content": 'Conversation info (untrusted metadata): ```json\n{"conversation_label": "群聊-1009999999"}\n```\nHello',
            }},
        ])
        key = eunuch._detect_session_key(jsonl)
        assert ":group:" in key
        assert "-1009999999" in key

    def test_detects_dm_session(self, tmp_path):
        eunuch._session_key_cache.clear()
        jsonl = tmp_path / "session.jsonl"
        self._write_jsonl(jsonl, [
            {"type": "message", "message": {
                "role": "user",
                "content": 'Sender (untrusted metadata): ```json\n{"name": "Alice"}\n```\nHello',
            }},
        ])
        key = eunuch._detect_session_key(jsonl)
        assert ":dm:" in key

    def test_defaults_to_main(self, tmp_path):
        eunuch._session_key_cache.clear()
        jsonl = tmp_path / "session.jsonl"
        self._write_jsonl(jsonl, [
            {"type": "message", "message": {"role": "user", "content": "plain message"}},
        ])
        key = eunuch._detect_session_key(jsonl)
        assert key == "agent:main:main"

    def test_caches_result(self, tmp_path):
        eunuch._session_key_cache.clear()
        jsonl = tmp_path / "session.jsonl"
        self._write_jsonl(jsonl, [
            {"type": "message", "message": {"role": "user", "content": "plain message"}},
        ])
        key1 = eunuch._detect_session_key(jsonl)
        key2 = eunuch._detect_session_key(jsonl)
        assert key1 == key2
        assert str(jsonl) in eunuch._session_key_cache
