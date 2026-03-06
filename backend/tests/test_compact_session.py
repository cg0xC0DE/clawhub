"""Tests for compact_session.py — session compaction and memory archival."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import compact_session as cs


# ── _extract_dialogue ─────────────────────────────────────────


class TestExtractDialogue:
    def _write_jsonl(self, path, entries):
        with open(path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def test_extracts_user_assistant_pairs(self, tmp_path):
        jsonl = tmp_path / "session.jsonl"
        self._write_jsonl(jsonl, [
            {"type": "session", "sessionId": "s1"},
            {"type": "message", "message": {"role": "user", "content": "这是一个较长的用户消息，需要超过十个字才能被提取"}},
            {"type": "message", "message": {"role": "assistant", "content": "这是一个较长的助手回复，同样需要足够长度"}},
        ])
        result = cs._extract_dialogue(jsonl)
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"

    def test_skips_short_messages(self, tmp_path):
        jsonl = tmp_path / "session.jsonl"
        self._write_jsonl(jsonl, [
            {"type": "message", "message": {"role": "user", "content": "hi"}},
        ])
        result = cs._extract_dialogue(jsonl)
        assert len(result) == 0  # too short (< 10 chars)

    def test_skips_heartbeat_ok(self, tmp_path):
        jsonl = tmp_path / "session.jsonl"
        self._write_jsonl(jsonl, [
            {"type": "message", "message": {"role": "assistant", "content": "HEARTBEAT_OK"}},
        ])
        result = cs._extract_dialogue(jsonl)
        assert len(result) == 0

    def test_skips_non_message_types(self, tmp_path):
        jsonl = tmp_path / "session.jsonl"
        self._write_jsonl(jsonl, [
            {"type": "session", "sessionId": "s1"},
            {"type": "toolCall", "tool": "read"},
            {"type": "message", "message": {"role": "system", "content": "系统消息不应被提取，忽略它"}},
            {"type": "message", "message": {"role": "user", "content": "这是一个较长的用户消息测试文本内容"}},
        ])
        result = cs._extract_dialogue(jsonl)
        assert len(result) == 1
        assert result[0]["role"] == "user"

    def test_handles_list_content(self, tmp_path):
        jsonl = tmp_path / "session.jsonl"
        self._write_jsonl(jsonl, [
            {"type": "message", "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "这是一段足够长的回复内容用于测试列表格式"},
                    {"type": "tool_use", "name": "read"},
                ],
            }},
        ])
        result = cs._extract_dialogue(jsonl)
        assert len(result) == 1
        assert "足够长" in result[0]["text"]

    def test_truncates_long_text(self, tmp_path):
        jsonl = tmp_path / "session.jsonl"
        long_text = "x" * 3000
        self._write_jsonl(jsonl, [
            {"type": "message", "message": {"role": "user", "content": long_text}},
        ])
        result = cs._extract_dialogue(jsonl)
        assert len(result[0]["text"]) == 2000

    def test_handles_missing_file(self):
        result = cs._extract_dialogue(Path("/nonexistent/file.jsonl"))
        assert result == []

    def test_handles_malformed_json(self, tmp_path):
        jsonl = tmp_path / "session.jsonl"
        jsonl.write_text('{"type":"message"}\n{invalid json}\n', encoding="utf-8")
        result = cs._extract_dialogue(jsonl)
        assert isinstance(result, list)  # should not crash


# ── _build_summary_prompt ─────────────────────────────────────


class TestBuildSummaryPrompt:
    def test_returns_single_user_message(self):
        dialogue = [
            {"role": "user", "text": "你好"},
            {"role": "assistant", "text": "你好！"},
        ]
        result = cs._build_summary_prompt(dialogue, "bot001")
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert "记忆整理" in result[0]["content"]

    def test_limits_to_60_turns(self):
        dialogue = [{"role": "user", "text": f"消息 {i}"} for i in range(100)]
        result = cs._build_summary_prompt(dialogue, "agent")
        # The prompt should only include last 60 turns
        assert "消息 99" in result[0]["content"]
        assert "消息 39" not in result[0]["content"]

    def test_truncates_individual_messages(self):
        dialogue = [{"role": "user", "text": "x" * 1000}]
        result = cs._build_summary_prompt(dialogue, "agent")
        # Each turn truncated to 500 chars in prompt
        content = result[0]["content"]
        assert len(content) < 1500  # system prompt + truncated content


# ── _parse_memory_entries ─────────────────────────────────────


class TestParseMemoryEntries:
    def test_parses_existing_entries(self):
        content = (
            "# MEMORY\n\nSome header text\n\n"
            "## 记忆条目\n\n"
            "- 2025-01-01 · event one\n"
            "- 2025-01-02 · event two\n"
        )
        header, entries = cs._parse_memory_entries(content)
        assert "# MEMORY" in header
        assert len(entries) == 2
        assert "event one" in entries[0]
        assert "event two" in entries[1]

    def test_no_section_marker(self):
        content = "# MEMORY\n\nJust some text"
        header, entries = cs._parse_memory_entries(content)
        assert "MEMORY" in header
        assert entries == []

    def test_empty_entries(self):
        content = "# MEMORY\n\n## 记忆条目\n\n"
        header, entries = cs._parse_memory_entries(content)
        assert entries == []

    def test_preserves_header(self):
        content = "# Header\n\nLine 1\nLine 2\n\n## 记忆条目\n\n- entry"
        header, entries = cs._parse_memory_entries(content)
        assert "Line 1" in header
        assert "Line 2" in header
        assert len(entries) == 1


# ── _update_memory_md ─────────────────────────────────────────


class TestUpdateMemoryMd:
    def test_creates_new_memory_file(self, tmp_path):
        memory_path = tmp_path / "MEMORY.md"
        new_bullets = "- 2025-01-01 · new event\n- 2025-01-02 · another"
        count = cs._update_memory_md(memory_path, new_bullets)
        assert count == 2
        assert memory_path.exists()
        content = memory_path.read_text(encoding="utf-8")
        assert "new event" in content
        assert "记忆条目" in content

    def test_appends_to_existing(self, tmp_path):
        memory_path = tmp_path / "MEMORY.md"
        memory_path.write_text(
            "# MEMORY\n\n## 记忆条目\n\n- old entry\n", encoding="utf-8"
        )
        count = cs._update_memory_md(memory_path, "- new entry")
        assert count == 2
        content = memory_path.read_text(encoding="utf-8")
        assert "old entry" in content
        assert "new entry" in content

    def test_fifo_overflow_archives(self, tmp_path):
        memory_path = tmp_path / "MEMORY.md"
        old_entries = "\n".join(f"- entry {i}" for i in range(50))
        memory_path.write_text(
            f"# MEMORY\n\n## 记忆条目\n\n{old_entries}\n", encoding="utf-8"
        )
        count = cs._update_memory_md(memory_path, "- new entry 50\n- new entry 51")
        assert count == 50  # capped at MEMORY_MAX_ENTRIES

        # Archive should have been created
        archive_dir = tmp_path / "memory" / "archive"
        assert archive_dir.exists()
        archive_files = list(archive_dir.glob("*.md"))
        assert len(archive_files) == 1

    def test_empty_input_returns_zero(self, tmp_path):
        memory_path = tmp_path / "MEMORY.md"
        count = cs._update_memory_md(memory_path, "")
        assert count == 0

    def test_ignores_non_bullet_lines(self, tmp_path):
        memory_path = tmp_path / "MEMORY.md"
        count = cs._update_memory_md(memory_path, "not a bullet\n- real bullet")
        assert count == 1


# ── _archive_old_entries ──────────────────────────────────────


class TestArchiveOldEntries:
    def test_creates_archive_file(self, tmp_path):
        archive_dir = tmp_path / "archive"
        cs._archive_old_entries(archive_dir, ["- entry 1", "- entry 2"])
        assert archive_dir.exists()
        files = list(archive_dir.glob("*.md"))
        assert len(files) == 1
        content = files[0].read_text(encoding="utf-8")
        assert "entry 1" in content
        assert "entry 2" in content

    def test_appends_to_existing_archive(self, tmp_path):
        archive_dir = tmp_path / "archive"
        cs._archive_old_entries(archive_dir, ["- first"])
        cs._archive_old_entries(archive_dir, ["- second"])
        files = list(archive_dir.glob("*.md"))
        assert len(files) == 1
        content = files[0].read_text(encoding="utf-8")
        assert "first" in content
        assert "second" in content


# ── _reset_session ────────────────────────────────────────────


class TestResetSession:
    def _write_jsonl(self, path, entries):
        with open(path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def test_preserves_last_n_lines(self, tmp_path):
        jsonl = tmp_path / "session.jsonl"
        entries = [{"type": "session", "sessionId": "s1"}]
        entries += [{"type": "message", "message": {"role": "user", "content": f"msg{i}"}} for i in range(20)]
        self._write_jsonl(jsonl, entries)

        result = cs._reset_session(jsonl)
        assert result == jsonl

        # New file should have session header + last CARRY_OVER_LINES
        with open(jsonl, "r", encoding="utf-8") as f:
            lines = f.readlines()
        # session header + carry-over lines
        assert len(lines) <= cs.CARRY_OVER_LINES + 1

        # Old file should be renamed to .reset.*
        reset_files = list(tmp_path.glob("*.reset.*"))
        assert len(reset_files) == 1

    def test_keeps_session_header(self, tmp_path):
        jsonl = tmp_path / "session.jsonl"
        entries = [
            {"type": "session", "sessionId": "test-session"},
            {"type": "message", "message": {"role": "user", "content": "hello there my friend"}},
        ]
        self._write_jsonl(jsonl, entries)

        cs._reset_session(jsonl)
        with open(jsonl, "r", encoding="utf-8") as f:
            first_line = f.readline()
        entry = json.loads(first_line)
        assert entry["type"] == "session"

    def test_handles_small_file(self, tmp_path):
        jsonl = tmp_path / "session.jsonl"
        entries = [{"type": "message", "message": {"role": "user", "content": "short file test"}}]
        self._write_jsonl(jsonl, entries)

        cs._reset_session(jsonl)
        assert jsonl.exists()
