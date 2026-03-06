"""Shared pytest fixtures for ClawHub backend tests."""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure backend/ is on sys.path for imports
BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture
def tmp_hub(tmp_path):
    """Create a minimal hub directory structure for testing."""
    # Create gateways/_template
    template_dir = tmp_path / "gateways" / "_template"
    template_dir.mkdir(parents=True)
    template_ws = template_dir / "workspace"
    template_ws.mkdir()

    # Minimal openclaw.json template
    template_cfg = {
        "meta": {"lastTouchedVersion": "2026.1.1"},
        "gateway": {
            "port": 60000,
            "auth": {"token": "PLACEHOLDER"},
        },
        "agents": {
            "defaults": {
                "workspace": "",
                "model": {"primary": "gpt-4o"},
            },
        },
        "channels": {
            "telegram": {"botToken": "PLACEHOLDER"},
        },
    }
    (template_dir / "openclaw.json").write_text(
        json.dumps(template_cfg, indent=2), encoding="utf-8"
    )

    # Workspace template files
    (template_ws / "SOUL.md").write_text("# SOUL\n__AGENT_NAME__", encoding="utf-8")
    (template_ws / "IDENTITY.md").write_text("# IDENTITY\n__AGENT_ID__", encoding="utf-8")
    (template_ws / "AGENTS.md").write_text("# AGENTS\n__AGENT_NAME__", encoding="utf-8")
    (template_ws / "HEARTBEAT.md").write_text("# HEARTBEAT", encoding="utf-8")
    (template_ws / "query_eunuch.py").write_text("# query eunuch script", encoding="utf-8")

    # palace.json
    palace = {
        "name": "Test Arena",
        "owner": {
            "id": "owner",
            "name": "测试用户",
            "aliases": ["用户", "主人"],
            "telegramUserId": "123456",
        },
        "herald": {
            "id": "herald",
            "name": "主持人",
            "botToken": "HERALD_TOKEN",
            "botUserId": "789",
        },
        "telegram": {
            "groupId": "-100999",
            "groupName": "测试群",
        },
        "proxy": "http://127.0.0.1:10020",
        "sync": {
            "enabled": True,
            "intervalSeconds": 30,
            "groupChat": {
                "enabled": True,
                "maxMessages": 30,
                "file": "GROUP_CHAT_LOG.md",
            },
            "whisper": {"enabled": True, "file": "WHISPERS.md"},
        },
        "participants": [
            {
                "id": "bot001",
                "name": "孙子",
                "botToken": "BOT001_TOKEN",
                "botUserId": "111",
            },
            {
                "id": "bot002",
                "name": "孔子",
                "botToken": "BOT002_TOKEN",
                "botUserId": "222",
            },
        ],
    }
    (tmp_path / "palace.json").write_text(
        json.dumps(palace, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # gateways.json
    gateways = [
        {
            "id": "herald",
            "name": "主持人",
            "emoji": "🦉",
            "port": 60008,
            "config_file": "gateways/herald/openclaw.json",
            "workspace_dir": "gateways/herald/state/workspace",
            "state_dir": "gateways/herald/state",
            "editable_files": [],
            "role": "herald",
        },
        {
            "id": "bot001",
            "name": "孙子",
            "emoji": "🦅",
            "port": 61001,
            "config_file": "gateways/bot001/openclaw.json",
            "workspace_dir": "gateways/bot001/state/workspace",
            "state_dir": "gateways/bot001/state",
            "editable_files": [],
            "role": "",
        },
    ]
    (tmp_path / "gateways.json").write_text(
        json.dumps(gateways, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return tmp_path


@pytest.fixture
def sample_palace():
    """Return a sample palace config dict."""
    return {
        "owner": {
            "id": "owner",
            "name": "测试用户",
            "aliases": ["用户", "主人"],
        },
        "participants": [
            {"id": "bot001", "name": "孙子", "aliases": ["兵圣"]},
            {"id": "bot002", "name": "孔子"},
        ],
    }
