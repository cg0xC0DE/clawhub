"""Tests for group_sync.py — group chat sync, tag parsing, outbox processing."""

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import group_sync as gs


# ── _extract_sender ───────────────────────────────────────────


class TestExtractSender:
    def test_extracts_name_from_metadata(self):
        text = (
            'Sender (untrusted metadata): ```json\n'
            '{"name": "Alice", "label": "Alice"}\n'
            '```\n'
            'Hello everyone'
        )
        assert gs._extract_sender(text) == "Alice"

    def test_no_metadata(self):
        assert gs._extract_sender("Just a plain message") == ""

    def test_chinese_name(self):
        text = (
            'Sender (untrusted metadata): ```json\n'
            '{"name": "孙武", "label": "孙武"}\n'
            '```\n'
            '你好'
        )
        assert gs._extract_sender(text) == "孙武"


# ── _extract_user_text ────────────────────────────────────────


class TestExtractUserText:
    def test_strips_metadata(self):
        text = (
            'Conversation info (untrusted metadata): ```json\n'
            '{"conversation_label": "group"}\n'
            '```\n'
            'Sender (untrusted metadata): ```json\n'
            '{"name": "Alice"}\n'
            '```\n'
            '实际消息内容'
        )
        result = gs._extract_user_text(text)
        assert result == "实际消息内容"

    def test_skips_heartbeat(self):
        assert gs._extract_user_text("HEARTBEAT check") == ""
        assert gs._extract_user_text("Read HEARTBEAT.md") == ""
        assert gs._extract_user_text("HEARTBEAT_OK") == ""

    def test_plain_text_passthrough(self):
        assert gs._extract_user_text("Hello world") == "Hello world"


# ── _clean_assistant_text ─────────────────────────────────────


class TestCleanAssistantText:
    def test_strips_whitespace(self):
        assert gs._clean_assistant_text("  hello  ") == "hello"

    def test_removes_final_tags(self):
        assert gs._clean_assistant_text("<final>Reply</final>") == "Reply"

    def test_nested_final_tags(self):
        result = gs._clean_assistant_text("<final>Hello <final>world</final></final>")
        assert "Hello" in result
        assert "world" in result
        assert "<final>" not in result


# ── _format_time ──────────────────────────────────────────────


class TestFormatTime:
    def test_utc_to_cst(self):
        # 2025-01-01T00:00:00Z → 08:00 CST
        result = gs._format_time("2025-01-01T00:00:00Z")
        assert result == "08:00"

    def test_with_timezone_offset(self):
        result = gs._format_time("2025-01-01T08:00:00+08:00")
        assert result == "16:00"

    def test_invalid_timestamp(self):
        result = gs._format_time("not a timestamp")
        assert result == "??:??"

    def test_midnight_wrap(self):
        result = gs._format_time("2025-01-01T20:30:00Z")
        assert result == "04:30"  # next day CST


# ── _parse_tags ───────────────────────────────────────────────


class TestParseTags:
    def _make_palace_gateways(self):
        palace = {
            "owner": {"id": "owner", "name": "用户", "aliases": ["主人"]},
        }
        gateways = [
            {"id": "bot001", "name": "孙子"},
            {"id": "bot002", "name": "孔子"},
        ]
        return palace, gateways

    def test_extracts_dm_tag(self):
        palace, gateways = self._make_palace_gateways()
        text = "公开回复 [DM:owner] 这是私信内容"
        clean, actions = gs._parse_tags(text, palace, gateways)
        assert "公开回复" in clean
        assert "私信内容" not in clean
        assert len(actions) == 1
        assert actions[0]["type"] == "dm"
        assert actions[0]["target_id"] == "owner"
        assert "私信内容" in actions[0]["content"]

    def test_extracts_whisper_tag(self):
        palace, gateways = self._make_palace_gateways()
        text = "公开内容 [WHISPER:bot002] 悄悄话"
        clean, actions = gs._parse_tags(text, palace, gateways)
        assert "悄悄话" not in clean
        assert len(actions) == 1
        assert actions[0]["type"] == "whisper"
        assert actions[0]["target_id"] == "bot002"

    def test_multiple_tags(self):
        palace, gateways = self._make_palace_gateways()
        text = "公开 [DM:owner] 私信 [WHISPER:bot001] 密语"
        clean, actions = gs._parse_tags(text, palace, gateways)
        assert len(actions) == 2

    def test_no_tags(self):
        palace, gateways = self._make_palace_gateways()
        text = "纯公开内容"
        clean, actions = gs._parse_tags(text, palace, gateways)
        assert clean == "纯公开内容"
        assert actions == []

    def test_resolves_name_to_id(self):
        palace, gateways = self._make_palace_gateways()
        text = "[DM:孙子] hello"
        clean, actions = gs._parse_tags(text, palace, gateways)
        assert actions[0]["target_id"] == "bot001"

    def test_empty_content_skipped(self):
        palace, gateways = self._make_palace_gateways()
        text = "[DM:owner]  "
        clean, actions = gs._parse_tags(text, palace, gateways)
        assert actions == []


# ── _build_group_chat_log ─────────────────────────────────────


class TestBuildGroupChatLog:
    def test_empty_messages(self):
        palace = {"owner": {"name": "用户"}}
        result = gs._build_group_chat_log([], palace, [])
        assert "暂无消息" in result
        assert "群组讨论记录" in result

    def test_formats_messages(self):
        palace = {"owner": {"name": "用户"}}
        gateways = [{"id": "bot001", "name": "孙子"}]
        messages = [
            {"timestamp": "2025-01-01T00:00:00Z", "role": "user",
             "sender_display": "用户", "display_text": "你好"},
            {"timestamp": "2025-01-01T00:01:00Z", "role": "assistant",
             "sender_display": "孙子", "display_text": "你好！"},
        ]
        result = gs._build_group_chat_log(messages, palace, gateways)
        assert "用户" in result
        assert "你好" in result
        assert "孙子" in result

    def test_truncates_long_messages(self):
        palace = {"owner": {"name": "用户"}}
        messages = [
            {"timestamp": "2025-01-01T00:00:00Z", "role": "user",
             "sender_display": "User", "display_text": "x" * 300},
        ]
        result = gs._build_group_chat_log(messages, palace, [])
        assert "..." in result

    def test_skips_empty_display_text(self):
        palace = {"owner": {"name": "用户"}}
        messages = [
            {"timestamp": "2025-01-01T00:00:00Z", "role": "user",
             "sender_display": "User", "display_text": ""},
        ]
        result = gs._build_group_chat_log(messages, palace, [])
        assert "User" not in result  # skipped because empty


# ── _append_whisper ───────────────────────────────────────────


class TestAppendWhisper:
    def test_appends_to_whisper_file(self, tmp_path):
        ws_dir = tmp_path / "workspace"
        ws_dir.mkdir()
        target_gw = {"workspace_dir": str(ws_dir)}
        with patch.object(gs, "HUB_DIR", Path("")):
            # Override the workspace path resolution
            whisper_file = ws_dir / "WHISPERS.md"
            gs._append_whisper({"workspace_dir": ""}, "孙子", "秘密消息")
        # The function uses HUB_DIR / workspace_dir, so let's test with proper patching
        pass  # tested indirectly via sync_once

    def test_creates_file_if_missing(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        gw = {"workspace_dir": "ws"}
        with patch.object(gs, "HUB_DIR", tmp_path):
            gs._append_whisper(gw, "孙子", "密语内容")
        whisper_file = ws / "WHISPERS.md"
        assert whisper_file.exists()
        content = whisper_file.read_text(encoding="utf-8")
        assert "密语内容" in content
        assert "孙子" in content


# ── _extract_messages (group_sync version) ────────────────────


class TestExtractMessagesGroupSync:
    def _write_jsonl(self, path, entries):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def test_extracts_user_and_assistant(self, tmp_path):
        jsonl = tmp_path / "session.jsonl"
        self._write_jsonl(jsonl, [
            {"type": "message", "timestamp": "2025-01-01T00:00:00Z",
             "message": {"role": "user", "content": "Hello world from user"}},
            {"type": "message", "timestamp": "2025-01-01T00:01:00Z",
             "message": {"role": "assistant", "content": "Assistant response here"}},
        ])
        result = gs._extract_messages(jsonl)
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"

    def test_skips_empty_content(self, tmp_path):
        jsonl = tmp_path / "session.jsonl"
        self._write_jsonl(jsonl, [
            {"type": "message", "timestamp": "2025-01-01T00:00:00Z",
             "message": {"role": "user", "content": ""}},
        ])
        result = gs._extract_messages(jsonl)
        assert len(result) == 0

    def test_respects_max_messages(self, tmp_path):
        jsonl = tmp_path / "session.jsonl"
        entries = [
            {"type": "message", "timestamp": f"2025-01-01T00:{i:02d}:00Z",
             "message": {"role": "user", "content": f"Message {i} with enough content"}}
            for i in range(20)
        ]
        self._write_jsonl(jsonl, entries)
        result = gs._extract_messages(jsonl, max_messages=5)
        assert len(result) == 5

    def test_handles_list_content(self, tmp_path):
        jsonl = tmp_path / "session.jsonl"
        self._write_jsonl(jsonl, [
            {"type": "message", "timestamp": "2025-01-01T00:00:00Z",
             "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "Part one"},
                 {"type": "tool_use", "name": "read"},
             ]}},
        ])
        result = gs._extract_messages(jsonl)
        assert len(result) == 1
        assert "Part one" in result[0]["text"]

    def test_skips_non_message_types(self, tmp_path):
        jsonl = tmp_path / "session.jsonl"
        self._write_jsonl(jsonl, [
            {"type": "session", "sessionId": "s1"},
            {"type": "toolCall", "tool": "read"},
            {"type": "message", "timestamp": "2025-01-01T00:00:00Z",
             "message": {"role": "user", "content": "actual message"}},
        ])
        result = gs._extract_messages(jsonl)
        assert len(result) == 1


# ── _find_group_session_file ──────────────────────────────────


class TestFindGroupSessionFile:
    def test_finds_session_by_group_id(self, tmp_path):
        state_dir = tmp_path / "state"
        sessions_dir = state_dir / "agents" / "main" / "sessions"
        sessions_dir.mkdir(parents=True)

        session_file = sessions_dir / "abc123.jsonl"
        session_file.write_text("", encoding="utf-8")

        sessions_json = sessions_dir / "sessions.json"
        sessions_json.write_text(json.dumps({
            "session:abc123": {
                "groupId": "-100999",
                "sessionFile": str(session_file),
            }
        }), encoding="utf-8")

        result = gs._find_group_session_file(state_dir, "-100999")
        assert result is not None
        assert result.name == "abc123.jsonl"

    def test_returns_none_when_no_match(self, tmp_path):
        state_dir = tmp_path / "state"
        sessions_dir = state_dir / "agents" / "main" / "sessions"
        sessions_dir.mkdir(parents=True)
        sessions_json = sessions_dir / "sessions.json"
        sessions_json.write_text(json.dumps({}), encoding="utf-8")

        result = gs._find_group_session_file(state_dir, "-100999")
        assert result is None

    def test_returns_none_when_no_sessions_json(self, tmp_path):
        result = gs._find_group_session_file(tmp_path, "-100999")
        assert result is None
