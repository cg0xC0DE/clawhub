"""Tests for gateway_manager.py — gateway lifecycle, provisioning, config IO."""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gateway_manager as gm


# ── _load_gateways_json / _save_gateways_json ─────────────────


class TestGatewaysJsonIO:
    def test_load_existing(self, tmp_path):
        gw_file = tmp_path / "gateways.json"
        data = [{"id": "bot001", "name": "孙子"}]
        gw_file.write_text(json.dumps(data), encoding="utf-8")
        with patch.object(gm, "GATEWAYS_FILE", gw_file):
            result = gm._load_gateways_json()
        assert len(result) == 1
        assert result[0]["id"] == "bot001"

    def test_load_missing_file(self, tmp_path):
        with patch.object(gm, "GATEWAYS_FILE", tmp_path / "nonexistent.json"):
            result = gm._load_gateways_json()
        assert result == []

    def test_load_with_bom(self, tmp_path):
        gw_file = tmp_path / "gateways.json"
        data = [{"id": "bot001"}]
        content = "\ufeff" + json.dumps(data)
        gw_file.write_text(content, encoding="utf-8")
        with patch.object(gm, "GATEWAYS_FILE", gw_file):
            result = gm._load_gateways_json()
        assert len(result) == 1

    def test_save(self, tmp_path):
        gw_file = tmp_path / "gateways.json"
        data = [{"id": "test", "name": "测试"}]
        with patch.object(gm, "GATEWAYS_FILE", gw_file):
            gm._save_gateways_json(data)
        loaded = json.loads(gw_file.read_text(encoding="utf-8"))
        assert loaded[0]["name"] == "测试"


# ── _read_openclaw_config ─────────────────────────────────────


class TestReadOpenclawConfig:
    def test_reads_valid_config(self, tmp_path):
        cfg_file = tmp_path / "openclaw.json"
        cfg_file.write_text(json.dumps({"gateway": {"port": 60001}}), encoding="utf-8")
        result = gm._read_openclaw_config(cfg_file)
        assert result["gateway"]["port"] == 60001

    def test_handles_bom(self, tmp_path):
        cfg_file = tmp_path / "openclaw.json"
        cfg_file.write_text("\ufeff" + json.dumps({"ok": True}), encoding="utf-8")
        result = gm._read_openclaw_config(cfg_file)
        assert result["ok"] is True

    def test_missing_file(self, tmp_path):
        result = gm._read_openclaw_config(tmp_path / "nope.json")
        assert result == {}

    def test_invalid_json(self, tmp_path):
        cfg_file = tmp_path / "openclaw.json"
        cfg_file.write_text("{invalid}", encoding="utf-8")
        result = gm._read_openclaw_config(cfg_file)
        assert result == {}


# ── _port_listening ───────────────────────────────────────────


class TestPortListening:
    def test_closed_port(self):
        assert gm._port_listening(59999) is False  # unlikely to be in use

    @patch("socket.create_connection")
    def test_open_port(self, mock_conn):
        mock_conn.return_value.__enter__ = lambda s: s
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)
        assert gm._port_listening(8080) is True


# ── GatewayProcess ────────────────────────────────────────────


class TestGatewayProcess:
    def _make_gw_def(self, **overrides):
        base = {
            "id": "bot001",
            "name": "孙子",
            "emoji": "🦅",
            "port": 61001,
            "config_file": "gateways/bot001/openclaw.json",
            "workspace_dir": "gateways/bot001/state/workspace",
            "state_dir": "gateways/bot001/state",
            "editable_files": [],
            "role": "",
        }
        base.update(overrides)
        return base

    def test_init_from_dict(self):
        gw = gm.GatewayProcess(self._make_gw_def())
        assert gw.id == "bot001"
        assert gw.name == "孙子"
        assert gw.port == 61001
        assert gw.status == "stopped"
        assert gw.pid is None

    def test_init_defaults(self):
        gw = gm.GatewayProcess({"id": "minimal"})
        assert gw.name == "minimal"
        assert gw.port == 0
        assert gw.emoji == "🪭"
        assert gw.role == ""

    def test_to_persist(self):
        gw = gm.GatewayProcess(self._make_gw_def())
        d = gw.to_persist()
        assert d["id"] == "bot001"
        assert d["name"] == "孙子"
        assert "status" not in d  # runtime field excluded
        assert "pid" not in d

    def test_to_dict_includes_status(self):
        gw = gm.GatewayProcess(self._make_gw_def())
        with patch.object(gw, "check_status", return_value="stopped"), \
             patch.object(gw, "_get_token", return_value=""):
            d = gw.to_dict()
        assert "status" in d
        assert "pid" in d
        assert d["token_configured"] is False

    def test_get_logs_empty(self):
        gw = gm.GatewayProcess(self._make_gw_def())
        lines, total, pruned = gw.get_logs()
        assert lines == []
        assert total == 0
        assert pruned == 0

    def test_emit_line_and_get_logs(self):
        gw = gm.GatewayProcess(self._make_gw_def())
        gw._emit_line(b"Hello world")
        gw._emit_line(b"\xe4\xbd\xa0\xe5\xa5\xbd")  # "你好" in utf-8
        lines, total, pruned = gw.get_logs()
        assert len(lines) == 2
        assert "Hello world" in lines[0]
        assert "你好" in lines[1]

    def test_log_pruning(self):
        gw = gm.GatewayProcess(self._make_gw_def())
        with patch.object(gm, "MAX_LOG_LINES", 3):
            for i in range(5):
                gw._emit_line(f"line {i}".encode())
        assert len(gw.log_buffer) == 3
        assert gw.log_pruned_count == 2

    def test_emit_line_strips_ansi(self):
        gw = gm.GatewayProcess(self._make_gw_def())
        gw._emit_line(b"\x1b[32mGreen text\x1b[0m")
        lines, _, _ = gw.get_logs()
        assert "\x1b" not in lines[0]
        assert "Green text" in lines[0]

    def test_stop_when_not_running(self):
        gw = gm.GatewayProcess(self._make_gw_def())
        with patch.object(gw, "_kill_by_port", return_value=False), \
             patch.object(gw, "_cleanup_openclaw_lock"):
            ok, msg = gw.stop()
        assert ok is False
        assert "not running" in msg

    def test_check_status_stopped(self):
        gw = gm.GatewayProcess(self._make_gw_def())
        with patch.object(gm, "_port_listening", return_value=False):
            status = gw.check_status()
        assert status == "stopped"

    def test_check_status_running_by_port(self):
        gw = gm.GatewayProcess(self._make_gw_def())
        with patch.object(gm, "_port_listening", return_value=True):
            status = gw.check_status()
        assert status == "running"

    def test_start_without_token(self, tmp_hub):
        gw_def = self._make_gw_def()
        gw = gm.GatewayProcess(gw_def)
        with patch.object(gm, "HUB_DIR", tmp_hub):
            # Create config without proper token
            cfg_path = tmp_hub / gw_def["config_file"]
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg = {"channels": {"telegram": {"botToken": "BOT001_TOKEN_HERE"}}}
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            ok, msg = gw.start()
        assert ok is False
        assert "Token" in msg


# ── GatewayManager ────────────────────────────────────────────


class TestGatewayManager:
    def test_load_from_file(self, tmp_path):
        gw_file = tmp_path / "gateways.json"
        data = [
            {"id": "bot001", "name": "孙子", "port": 61001},
            {"id": "bot002", "name": "孔子", "port": 61002},
        ]
        gw_file.write_text(json.dumps(data), encoding="utf-8")
        with patch.object(gm, "GATEWAYS_FILE", gw_file):
            mgr = gm.GatewayManager()
        assert len(mgr.list_all()) == 2

    def test_get_existing(self, tmp_path):
        gw_file = tmp_path / "gateways.json"
        gw_file.write_text(json.dumps([{"id": "bot001", "port": 61001}]), encoding="utf-8")
        with patch.object(gm, "GATEWAYS_FILE", gw_file):
            mgr = gm.GatewayManager()
        assert mgr.get("bot001") is not None
        assert mgr.get("nonexistent") is None

    def test_add_gateway(self, tmp_path):
        gw_file = tmp_path / "gateways.json"
        gw_file.write_text("[]", encoding="utf-8")
        with patch.object(gm, "GATEWAYS_FILE", gw_file):
            mgr = gm.GatewayManager()
            gw = mgr.add({"id": "new_gw", "name": "新网关", "port": 62000})
        assert gw.id == "new_gw"
        assert mgr.get("new_gw") is not None
        # Check persisted
        saved = json.loads(gw_file.read_text(encoding="utf-8"))
        assert any(g["id"] == "new_gw" for g in saved)

    def test_remove_gateway(self, tmp_path):
        gw_file = tmp_path / "gateways.json"
        gw_file.write_text(json.dumps([{"id": "bot001", "port": 61001}]), encoding="utf-8")
        with patch.object(gm, "GATEWAYS_FILE", gw_file):
            mgr = gm.GatewayManager()
            with patch.object(mgr.gateways["bot001"], "stop", return_value=(False, "not running")):
                result = mgr.remove("bot001")
        assert result is True
        assert mgr.get("bot001") is None

    def test_remove_nonexistent(self, tmp_path):
        gw_file = tmp_path / "gateways.json"
        gw_file.write_text("[]", encoding="utf-8")
        with patch.object(gm, "GATEWAYS_FILE", gw_file):
            mgr = gm.GatewayManager()
            result = mgr.remove("nope")
        assert result is False

    def test_update_gateway(self, tmp_path):
        gw_file = tmp_path / "gateways.json"
        gw_file.write_text(json.dumps([{"id": "bot001", "name": "旧名", "port": 61001}]), encoding="utf-8")
        with patch.object(gm, "GATEWAYS_FILE", gw_file):
            mgr = gm.GatewayManager()
            updated = mgr.update("bot001", {"name": "新名字"})
        assert updated is not None
        assert updated.name == "新名字"

    def test_reload_updates_metadata(self, tmp_path):
        gw_file = tmp_path / "gateways.json"
        gw_file.write_text(json.dumps([{"id": "bot001", "name": "旧名", "port": 61001}]), encoding="utf-8")
        with patch.object(gm, "GATEWAYS_FILE", gw_file):
            mgr = gm.GatewayManager()
            # Update file
            gw_file.write_text(json.dumps([{"id": "bot001", "name": "新名", "port": 61002}]), encoding="utf-8")
            mgr.reload()
        assert mgr.get("bot001").name == "新名"
        assert mgr.get("bot001").port == 61002

    def test_reload_removes_deleted_gateways(self, tmp_path):
        gw_file = tmp_path / "gateways.json"
        gw_file.write_text(json.dumps([
            {"id": "bot001", "port": 61001},
            {"id": "bot002", "port": 61002},
        ]), encoding="utf-8")
        with patch.object(gm, "GATEWAYS_FILE", gw_file):
            mgr = gm.GatewayManager()
            # Remove bot002 from file
            gw_file.write_text(json.dumps([{"id": "bot001", "port": 61001}]), encoding="utf-8")
            with patch.object(mgr.gateways["bot002"], "stop", return_value=(False, "not running")):
                mgr.reload()
        assert mgr.get("bot002") is None

    def test_reload_adds_new_gateways(self, tmp_path):
        gw_file = tmp_path / "gateways.json"
        gw_file.write_text(json.dumps([{"id": "bot001", "port": 61001}]), encoding="utf-8")
        with patch.object(gm, "GATEWAYS_FILE", gw_file):
            mgr = gm.GatewayManager()
            gw_file.write_text(json.dumps([
                {"id": "bot001", "port": 61001},
                {"id": "bot003", "port": 61003},
            ]), encoding="utf-8")
            mgr.reload()
        assert mgr.get("bot003") is not None


# ── provision_gateway ─────────────────────────────────────────


class TestProvisionGateway:
    def test_creates_gateway_dir(self, tmp_hub):
        with patch.object(gm, "GATEWAYS_DIR", tmp_hub / "gateways"), \
             patch.object(gm, "_copy_auth_profiles"):
            cfg_path = gm.provision_gateway("new_bot", "新角色", "🐉", 62000)
        assert cfg_path.exists()
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert cfg["gateway"]["port"] == 62000

    def test_skips_existing_config(self, tmp_hub):
        gw_dir = tmp_hub / "gateways" / "existing"
        gw_dir.mkdir(parents=True)
        cfg_path = gw_dir / "openclaw.json"
        cfg_path.write_text("{}", encoding="utf-8")
        with patch.object(gm, "GATEWAYS_DIR", tmp_hub / "gateways"):
            result = gm.provision_gateway("existing", "旧角色", "🐉", 62000)
        assert result == cfg_path

    def test_sets_primary_model(self, tmp_hub):
        with patch.object(gm, "GATEWAYS_DIR", tmp_hub / "gateways"), \
             patch.object(gm, "_copy_auth_profiles"):
            cfg_path = gm.provision_gateway("bot_model", "角色", "🐉", 62000,
                                             primary_model="claude-sonnet-4")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert cfg["agents"]["defaults"]["model"]["primary"] == "claude-sonnet-4"

    def test_generates_unique_token(self, tmp_hub):
        with patch.object(gm, "GATEWAYS_DIR", tmp_hub / "gateways"), \
             patch.object(gm, "_copy_auth_profiles"):
            cfg1 = gm.provision_gateway("bot_a", "A", "🐉", 62001)
            cfg2 = gm.provision_gateway("bot_b", "B", "🐉", 62002)
        t1 = json.loads(cfg1.read_text(encoding="utf-8"))["gateway"]["auth"]["token"]
        t2 = json.loads(cfg2.read_text(encoding="utf-8"))["gateway"]["auth"]["token"]
        assert t1 != t2


# ── _provision_workspace_scripts ──────────────────────────────


class TestProvisionWorkspaceScripts:
    def test_copies_and_substitutes(self, tmp_hub):
        ws_dir = tmp_hub / "gateways" / "new_bot" / "state" / "workspace"
        ws_dir.mkdir(parents=True)
        with patch.object(gm, "GATEWAYS_DIR", tmp_hub / "gateways"):
            gm._provision_workspace_scripts("new_bot", "新角色", ws_dir)
        soul = (ws_dir / "SOUL.md").read_text(encoding="utf-8")
        assert "新角色" in soul
        identity = (ws_dir / "IDENTITY.md").read_text(encoding="utf-8")
        assert "new_bot" in identity

    def test_skips_existing_files(self, tmp_hub):
        ws_dir = tmp_hub / "gateways" / "bot" / "state" / "workspace"
        ws_dir.mkdir(parents=True)
        (ws_dir / "SOUL.md").write_text("custom content", encoding="utf-8")
        with patch.object(gm, "GATEWAYS_DIR", tmp_hub / "gateways"):
            gm._provision_workspace_scripts("bot", "角色", ws_dir)
        assert (ws_dir / "SOUL.md").read_text(encoding="utf-8") == "custom content"


# ── cleanup_gateway_files ─────────────────────────────────────


class TestCleanupGatewayFiles:
    def test_removes_gateway_dir(self, tmp_path):
        gw_dir = tmp_path / "gateways" / "doomed"
        gw_dir.mkdir(parents=True)
        (gw_dir / "openclaw.json").write_text("{}", encoding="utf-8")
        with patch.object(gm, "GATEWAYS_DIR", tmp_path / "gateways"):
            cleaned = gm.cleanup_gateway_files("doomed")
        assert len(cleaned) >= 1
        assert not gw_dir.exists()

    def test_nonexistent_gateway(self, tmp_path):
        with patch.object(gm, "GATEWAYS_DIR", tmp_path / "gateways"):
            cleaned = gm.cleanup_gateway_files("nope")
        assert cleaned == []
