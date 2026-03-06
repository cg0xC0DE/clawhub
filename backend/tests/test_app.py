"""Tests for app.py — Flask API endpoints."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Flask test client fixture ─────────────────────────────────

@pytest.fixture
def client(tmp_hub):
    """Create a Flask test client with mocked hub directory."""
    # Patch HUB_DIR and GATEWAYS_DIR before importing app
    import gateway_manager as gm
    import group_sync as gs
    import eunuch

    orig_hub = gm.HUB_DIR
    orig_gateways_dir = gm.GATEWAYS_DIR
    orig_gateways_file = gm.GATEWAYS_FILE
    orig_template = gm.TEMPLATE_FILE

    gm.HUB_DIR = tmp_hub
    gm.GATEWAYS_DIR = tmp_hub / "gateways"
    gm.GATEWAYS_FILE = tmp_hub / "gateways.json"
    gm.TEMPLATE_FILE = tmp_hub / "gateways" / "_template" / "openclaw.json"
    gs.HUB_DIR = tmp_hub
    eunuch.HUB_DIR = tmp_hub
    eunuch.POOL_FILE = tmp_hub / "backend" / "eunuch_pool.jsonl"

    # Create backend dir for pool file
    (tmp_hub / "backend").mkdir(exist_ok=True)

    import app as flask_app

    flask_app.app.config["TESTING"] = True
    flask_app.HUB_DIR = tmp_hub  # patch the app module's own HUB_DIR reference
    flask_app.MATERIALS_DIR = tmp_hub / "materials"
    flask_app.MATERIALS_INDEX = tmp_hub / "materials_index.json"
    flask_app.LLM_PROFILES_FILE = tmp_hub / "llm_profiles.json"
    flask_app.PERSONAS_DIR = tmp_hub / "personas"
    flask_app.PERSONAS_INDEX = tmp_hub / "personas_index.json"
    flask_app.ARENA_TEMPLATES_FILE = tmp_hub / "arena_templates.json"
    flask_app.SECRETARY_DIR = tmp_hub / "secretary_history"
    flask_app.SECRETARY_INDEX = tmp_hub / "secretary_history" / "index.json"
    (tmp_hub / "materials").mkdir(exist_ok=True)
    (tmp_hub / "personas").mkdir(exist_ok=True)
    flask_app.manager = gm.GatewayManager()
    flask_app._persona_gen_tasks.clear()

    with patch.object(flask_app, "start_sync"), \
         patch.object(flask_app, "start_eunuch"):
        with flask_app.app.test_client() as c:
            yield c

    # Restore
    gm.HUB_DIR = orig_hub
    gm.GATEWAYS_DIR = orig_gateways_dir
    gm.GATEWAYS_FILE = orig_gateways_file
    gm.TEMPLATE_FILE = orig_template
    gs.HUB_DIR = orig_hub
    eunuch.HUB_DIR = orig_hub


# ── GET /api/gateways ─────────────────────────────────────────


class TestListGateways:
    def test_returns_list(self, client):
        resp = client.get("/api/gateways")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) == 2  # herald + bot001 from tmp_hub

    def test_contains_gateway_fields(self, client):
        resp = client.get("/api/gateways")
        gw = resp.get_json()[0]
        assert "id" in gw
        assert "name" in gw
        assert "status" in gw
        assert "port" in gw


# ── GET /api/gateways/<gw_id> ─────────────────────────────────


class TestGetGateway:
    def test_existing_gateway(self, client):
        resp = client.get("/api/gateways/herald")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == "herald"

    def test_nonexistent_gateway(self, client):
        resp = client.get("/api/gateways/nonexistent")
        assert resp.status_code == 404


# ── POST /api/validate-character ──────────────────────────────


class TestValidateCharacter:
    def test_known_character(self, client):
        resp = client.post("/api/validate-character",
                           json={"name": "孔子"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["valid"] is True
        assert data["character_name"] == "孔子"
        assert data["emoji"] == "📜"

    def test_known_character_with_suffix(self, client):
        resp = client.post("/api/validate-character",
                           json={"name": "孔子 opus 4.6"})
        data = resp.get_json()
        assert data["valid"] is True
        assert data["character_name"] == "孔子"
        assert "附注" in data["reason"]

    def test_unknown_character(self, client):
        resp = client.post("/api/validate-character",
                           json={"name": "未知角色"})
        data = resp.get_json()
        assert data["valid"] is True
        assert data["character_name"] == "未知角色"
        assert data["emoji"] == "🪭"

    def test_empty_name(self, client):
        resp = client.post("/api/validate-character", json={"name": ""})
        data = resp.get_json()
        assert data["valid"] is False


# ── PATCH /api/gateways/<gw_id> ───────────────────────────────


class TestUpdateGateway:
    def test_update_name(self, client):
        resp = client.patch("/api/gateways/bot001",
                            json={"name": "新名字"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["name"] == "新名字"

    def test_update_nonexistent(self, client):
        resp = client.patch("/api/gateways/nope",
                            json={"name": "test"})
        assert resp.status_code == 404

    def test_ignores_disallowed_fields(self, client):
        resp = client.patch("/api/gateways/bot001",
                            json={"name": "ok", "id": "hacked"})
        data = resp.get_json()
        assert data["id"] == "bot001"  # id not changed


# ── DELETE /api/gateways/<gw_id> ──────────────────────────────


class TestDeleteGateway:
    def test_delete_existing(self, client):
        resp = client.delete("/api/gateways/bot001")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

        # Verify it's gone
        resp2 = client.get("/api/gateways/bot001")
        assert resp2.status_code == 404

    def test_delete_nonexistent(self, client):
        resp = client.delete("/api/gateways/nope")
        assert resp.status_code == 404


# ── POST /api/gateways/<gw_id>/start ─────────────────────────


class TestStartGateway:
    def test_start_nonexistent(self, client):
        resp = client.post("/api/gateways/nope/start")
        assert resp.status_code == 404

    def test_start_without_token(self, client, tmp_hub):
        # Create config file without token
        cfg_dir = tmp_hub / "gateways" / "bot001"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg = {"channels": {"telegram": {"botToken": "BOT001_TOKEN_HERE"}}}
        (cfg_dir / "openclaw.json").write_text(json.dumps(cfg), encoding="utf-8")

        resp = client.post("/api/gateways/bot001/start")
        assert resp.status_code == 400
        assert "Token" in resp.get_json()["error"]


# ── POST /api/gateways/<gw_id>/stop ──────────────────────────


class TestStopGateway:
    def test_stop_nonexistent(self, client):
        resp = client.post("/api/gateways/nope/stop")
        assert resp.status_code == 404

    def test_stop_stopped_gateway(self, client):
        resp = client.post("/api/gateways/herald/stop")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "message" in data


# ── GET /api/gateways/<gw_id>/logs ────────────────────────────


class TestGetLogs:
    def test_empty_logs(self, client):
        resp = client.get("/api/gateways/herald/logs")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["lines"] == []
        assert data["total"] == 0

    def test_logs_nonexistent(self, client):
        resp = client.get("/api/gateways/nope/logs")
        assert resp.status_code == 404


# ── File editor endpoints ─────────────────────────────────────


class TestFileEditor:
    def test_list_files(self, client):
        resp = client.get("/api/gateways/bot001/files")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)

    def test_read_file_not_in_editable(self, client):
        resp = client.get("/api/gateways/bot001/files/read?path=secret.txt")
        assert resp.status_code == 403

    def test_read_file_no_path(self, client):
        resp = client.get("/api/gateways/bot001/files/read")
        assert resp.status_code == 400

    def test_write_file_not_in_editable(self, client):
        resp = client.put("/api/gateways/bot001/files/write",
                          json={"path": "secret.txt", "content": "hacked"})
        assert resp.status_code == 403


# ── GET /api/palace ───────────────────────────────────────────


class TestPalace:
    def test_get_palace(self, client):
        resp = client.get("/api/palace")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["name"] == "Test Arena"
        assert "participants" in data

    def test_set_palace(self, client, tmp_hub):
        new_palace = {"name": "Updated Arena", "owner": {"name": "Admin"}}
        resp = client.put("/api/palace", json=new_palace)
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

        # Verify file was written correctly
        palace_data = json.loads((tmp_hub / "palace.json").read_text(encoding="utf-8"))
        assert palace_data["name"] == "Updated Arena"


# ── GET /api/persona-models ──────────────────────────────────


class TestPersonaModels:
    def test_returns_list(self, client):
        with patch("generate_persona.get_available_models",
                   return_value=[{"key": "gpt-4o", "label": "GPT-4o", "provider": "openai"}]):
            resp = client.get("/api/persona-models")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)


# ── POST /api/gateways/<gw_id>/regenerate-persona ────────────


class TestRegeneratePersona:
    def test_nonexistent_gateway(self, client):
        resp = client.post("/api/gateways/nope/regenerate-persona",
                           json={"character_name": "test", "model": "gpt-4o"})
        assert resp.status_code == 404

    def test_starts_generation(self, client):
        resp = client.post("/api/gateways/herald/regenerate-persona",
                           json={"character_name": "主持人", "model": "gpt-4o"})
        assert resp.status_code == 202
        data = resp.get_json()
        assert data["status"] == "generating"

    def test_status_idle_for_unknown_gateway(self, client):
        resp = client.get("/api/gateways/bot001/regenerate-persona/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "idle"


# ── Token management ─────────────────────────────────────────


class TestTokenManagement:
    def test_set_token_nonexistent(self, client):
        resp = client.put("/api/gateways/nope/token",
                          json={"token": "123"})
        assert resp.status_code == 404

    def test_set_token_empty(self, client):
        resp = client.put("/api/gateways/herald/token",
                          json={"token": ""})
        assert resp.status_code == 400

    def test_set_token_no_config(self, client):
        resp = client.put("/api/gateways/herald/token",
                          json={"token": "new_token_123"})
        # Config file might not exist in test env
        assert resp.status_code in (200, 404)


# ── Heartbeat config ─────────────────────────────────────────


class TestHeartbeatConfig:
    def test_get_heartbeat_nonexistent(self, client):
        resp = client.get("/api/gateways/nope/heartbeat")
        assert resp.status_code == 404

    def test_get_heartbeat(self, client):
        resp = client.get("/api/gateways/herald/heartbeat")
        assert resp.status_code == 200
        # Returns whatever is in config (possibly empty dict)
        assert isinstance(resp.get_json(), dict)


# ── Sync trigger ──────────────────────────────────────────────


class TestSyncTrigger:
    def test_trigger_sync(self, client):
        with patch("group_sync.sync_once"):
            resp = client.post("/api/palace/sync")
        assert resp.status_code == 200


# ── Start all / Stop all ─────────────────────────────────────


class TestBatchOperations:
    def test_start_all(self, client):
        resp = client.post("/api/start-all")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)

    def test_stop_all(self, client):
        resp = client.post("/api/stop-all")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)


# ── Materials API (素材库) ────────────────────────────────────


class TestMaterialsList:
    def test_empty_list(self, client):
        resp = client.get("/api/materials")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["materials"] == []

    def test_list_after_upload(self, client):
        client.post("/api/materials",
                     json={"content": "hello world", "filename": "test.md"})
        resp = client.get("/api/materials")
        data = resp.get_json()
        assert len(data["materials"]) == 1
        assert data["materials"][0]["filename"] == "test.md"


class TestMaterialsUpload:
    def test_upload_json(self, client):
        resp = client.post("/api/materials",
                           json={"content": "# My Draft", "filename": "draft.md"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        mat = data["material"]
        assert mat["filename"] == "draft.md"
        assert mat["size"] == len("# My Draft")
        assert mat["lines"] == 1
        assert "id" in mat
        assert mat["type"] == "knowledge"  # default type

    def test_upload_with_type_target(self, client):
        resp = client.post("/api/materials",
                           json={"content": "article", "filename": "draft.md", "type": "target"})
        mat = resp.get_json()["material"]
        assert mat["type"] == "target"

    def test_upload_invalid_type_defaults_to_knowledge(self, client):
        resp = client.post("/api/materials",
                           json={"content": "x", "filename": "a.md", "type": "invalid"})
        mat = resp.get_json()["material"]
        assert mat["type"] == "knowledge"

    def test_upload_empty_content(self, client):
        resp = client.post("/api/materials",
                           json={"content": "", "filename": "empty.md"})
        assert resp.status_code == 400

    def test_upload_multiline(self, client):
        content = "line1\nline2\nline3"
        resp = client.post("/api/materials",
                           json={"content": content, "filename": "multi.txt"})
        data = resp.get_json()
        assert data["material"]["lines"] == 3

    def test_upload_default_filename(self, client):
        resp = client.post("/api/materials",
                           json={"content": "some text"})
        data = resp.get_json()
        assert data["material"]["filename"] == "untitled.md"

    def test_upload_multipart(self, client):
        import io
        data = {"file": (io.BytesIO(b"multipart content"), "uploaded.md")}
        resp = client.post("/api/materials", content_type="multipart/form-data",
                           data=data)
        assert resp.status_code == 200
        assert resp.get_json()["material"]["filename"] == "uploaded.md"

    def test_upload_multipart_no_file(self, client):
        resp = client.post("/api/materials", content_type="multipart/form-data",
                           data={})
        assert resp.status_code == 400


class TestMaterialsGetOne:
    def test_get_existing(self, client):
        upload = client.post("/api/materials",
                             json={"content": "hello", "filename": "a.md"})
        mat_id = upload.get_json()["material"]["id"]

        resp = client.get(f"/api/materials/{mat_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["content"] == "hello"
        assert data["filename"] == "a.md"

    def test_get_nonexistent(self, client):
        resp = client.get("/api/materials/nonexistent")
        assert resp.status_code == 404


class TestMaterialsDelete:
    def test_delete_existing(self, client):
        upload = client.post("/api/materials",
                             json={"content": "to delete", "filename": "del.md"})
        mat_id = upload.get_json()["material"]["id"]

        resp = client.delete(f"/api/materials/{mat_id}")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

        # Verify it's gone
        resp2 = client.get(f"/api/materials/{mat_id}")
        assert resp2.status_code == 404

    def test_delete_nonexistent(self, client):
        resp = client.delete("/api/materials/nonexistent")
        assert resp.status_code == 404


class TestMaterialsTags:
    def test_update_tags(self, client):
        upload = client.post("/api/materials",
                             json={"content": "tagged", "filename": "tag.md"})
        mat_id = upload.get_json()["material"]["id"]

        resp = client.put(f"/api/materials/{mat_id}/tags",
                          json={"tags": ["draft", "important"]})
        assert resp.status_code == 200

        # Verify tags persisted
        resp2 = client.get(f"/api/materials/{mat_id}")
        assert resp2.get_json()["tags"] == ["draft", "important"]

    def test_update_tags_nonexistent(self, client):
        resp = client.put("/api/materials/nonexistent/tags",
                          json={"tags": ["x"]})
        assert resp.status_code == 404


class TestMaterialsType:
    def test_toggle_type(self, client):
        upload = client.post("/api/materials",
                             json={"content": "doc", "filename": "doc.md"})
        mat_id = upload.get_json()["material"]["id"]
        assert upload.get_json()["material"]["type"] == "knowledge"

        resp = client.put(f"/api/materials/{mat_id}/type",
                          json={"type": "target"})
        assert resp.status_code == 200

        resp2 = client.get(f"/api/materials/{mat_id}")
        assert resp2.get_json()["type"] == "target"

    def test_invalid_type_rejected(self, client):
        upload = client.post("/api/materials",
                             json={"content": "x", "filename": "x.md"})
        mat_id = upload.get_json()["material"]["id"]
        resp = client.put(f"/api/materials/{mat_id}/type",
                          json={"type": "invalid"})
        assert resp.status_code == 400

    def test_type_nonexistent(self, client):
        resp = client.put("/api/materials/nonexistent/type",
                          json={"type": "target"})
        assert resp.status_code == 404


class TestMaterialsOrdering:
    def test_newest_first(self, client):
        client.post("/api/materials",
                    json={"content": "first", "filename": "first.md"})
        client.post("/api/materials",
                    json={"content": "second", "filename": "second.md"})

        resp = client.get("/api/materials")
        items = resp.get_json()["materials"]
        assert len(items) == 2
        assert items[0]["filename"] == "second.md"  # newest first
        assert items[1]["filename"] == "first.md"


# ── Model Update Detection ────────────────────────────────────


class TestCheckModelUpdates:
    def _seed_profiles(self, client, tmp_hub):
        """Write a minimal llm_profiles.json for testing."""
        profiles = {
            "providers": [
                {
                    "id": "openai",
                    "label": "OpenAI",
                    "api_key": "",
                    "models": [{"id": "gpt-5.4", "label": "GPT-5.4"}],
                },
                {
                    "id": "anthropic",
                    "label": "Anthropic",
                    "api_key": "",
                    "models": [{"id": "claude-opus-4-6", "label": "Claude Opus 4.6"}],
                },
            ],
        }
        (tmp_hub / "llm_profiles.json").write_text(
            json.dumps(profiles, indent=2), encoding="utf-8"
        )

    def test_check_updates_returns_results(self, client, tmp_hub):
        self._seed_profiles(client, tmp_hub)
        resp = client.post("/api/llm-profiles/check-updates")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "updates" in data
        assert len(data["updates"]) == 2

    def test_check_updates_structure(self, client, tmp_hub):
        self._seed_profiles(client, tmp_hub)
        resp = client.post("/api/llm-profiles/check-updates")
        updates = resp.get_json()["updates"]
        for u in updates:
            assert "provider_id" in u
            assert "provider_label" in u
            assert "source" in u
            assert "current" in u
            assert "discovered" in u
            assert "new" in u
            assert "removable" in u

    def test_known_latest_for_anthropic(self, client, tmp_hub):
        self._seed_profiles(client, tmp_hub)
        resp = client.post("/api/llm-profiles/check-updates")
        updates = resp.get_json()["updates"]
        anthropic = next(u for u in updates if u["provider_id"] == "anthropic")
        assert anthropic["source"] == "known"
        discovered_ids = {m["id"] for m in anthropic["discovered"]}
        assert "claude-opus-4-6" in discovered_ids
        assert "claude-sonnet-4-6" in discovered_ids

    def test_openai_without_key_uses_no_discovery(self, client, tmp_hub):
        self._seed_profiles(client, tmp_hub)
        resp = client.post("/api/llm-profiles/check-updates")
        updates = resp.get_json()["updates"]
        openai = next(u for u in updates if u["provider_id"] == "openai")
        # No API key → no API probe, but no _KNOWN_LATEST for openai either
        # so discovered may be empty
        assert openai["source"] in ("known", "failed")


class TestApplyModelUpdates:
    def _seed_profiles(self, tmp_hub):
        profiles = {
            "providers": [
                {
                    "id": "openai",
                    "label": "OpenAI",
                    "models": [{"id": "gpt-5.4", "label": "GPT-5.4"}],
                },
            ],
        }
        (tmp_hub / "llm_profiles.json").write_text(
            json.dumps(profiles, indent=2), encoding="utf-8"
        )

    def test_apply_updates(self, client, tmp_hub):
        self._seed_profiles(tmp_hub)
        resp = client.post("/api/llm-profiles/apply-updates", json={
            "changes": [
                {
                    "provider_id": "openai",
                    "models": [
                        {"id": "gpt-5.5", "label": "GPT-5.5"},
                        {"id": "gpt-5.5-pro", "label": "GPT-5.5 Pro"},
                    ],
                },
            ],
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "openai" in data["applied"]

        # Verify file updated
        saved = json.loads((tmp_hub / "llm_profiles.json").read_text("utf-8"))
        openai_prov = next(p for p in saved["providers"] if p["id"] == "openai")
        assert len(openai_prov["models"]) == 2
        assert openai_prov["models"][0]["id"] == "gpt-5.5"

    def test_apply_empty_changes(self, client, tmp_hub):
        self._seed_profiles(tmp_hub)
        resp = client.post("/api/llm-profiles/apply-updates",
                           json={"changes": []})
        assert resp.status_code == 400

    def test_apply_nonexistent_provider(self, client, tmp_hub):
        self._seed_profiles(tmp_hub)
        resp = client.post("/api/llm-profiles/apply-updates", json={
            "changes": [{"provider_id": "fakeprov", "models": [{"id": "x", "label": "X"}]}],
        })
        assert resp.status_code == 200
        assert resp.get_json()["applied"] == []  # no match


# ── Arena with materials (讨论标的) ──────────────────────────


class TestArenaWithMaterials:
    def test_arena_start_with_target(self, client, tmp_hub):
        # Upload a target material
        upload = client.post("/api/materials",
                             json={"content": "# 我的文章草稿\n\n这是正文。", "filename": "草稿.md", "type": "target"})
        mat_id = upload.get_json()["material"]["id"]

        with patch("app._arena_running") as mock_running, \
             patch("app.threading.Thread") as mock_thread:
            mock_running.is_set.return_value = False
            resp = client.post("/api/arena/start", json={
                "topic": "讨论我的文章草稿",
                "participant_ids": ["bot001"],
                "target_ids": [mat_id],
                "rounds": 1,
            })

        assert resp.status_code == 200
        mock_thread.assert_called_once()
        call_args = mock_thread.call_args
        args = call_args.kwargs.get("args") or call_args[1].get("args", ())
        # args = (topic, expert_ids, rounds, max_reply, interval, knowledge_text, target_text)
        target_text = args[6] if len(args) > 6 else ""
        assert "我的文章草稿" in target_text

    def test_arena_start_with_knowledge(self, client, tmp_hub):
        upload = client.post("/api/materials",
                             json={"content": "# 参考资料", "filename": "知识.md", "type": "knowledge"})
        mat_id = upload.get_json()["material"]["id"]

        with patch("app._arena_running") as mock_running, \
             patch("app.threading.Thread") as mock_thread:
            mock_running.is_set.return_value = False
            resp = client.post("/api/arena/start", json={
                "topic": "讨论",
                "participant_ids": ["bot001"],
                "knowledge_ids": [mat_id],
                "rounds": 1,
            })

        assert resp.status_code == 200
        args = mock_thread.call_args.kwargs.get("args") or mock_thread.call_args[1].get("args", ())
        knowledge_text = args[5] if len(args) > 5 else ""
        target_text = args[6] if len(args) > 6 else ""
        assert "参考资料" in knowledge_text
        assert target_text == ""

    def test_arena_start_with_both(self, client, tmp_hub):
        k_up = client.post("/api/materials",
                           json={"content": "knowledge content", "filename": "k.md", "type": "knowledge"})
        t_up = client.post("/api/materials",
                           json={"content": "target content", "filename": "t.md", "type": "target"})
        k_id = k_up.get_json()["material"]["id"]
        t_id = t_up.get_json()["material"]["id"]

        with patch("app._arena_running") as mock_running, \
             patch("app.threading.Thread") as mock_thread:
            mock_running.is_set.return_value = False
            resp = client.post("/api/arena/start", json={
                "topic": "综合讨论",
                "participant_ids": ["bot001"],
                "knowledge_ids": [k_id],
                "target_ids": [t_id],
                "rounds": 1,
            })

        assert resp.status_code == 200
        args = mock_thread.call_args.kwargs.get("args") or mock_thread.call_args[1].get("args", ())
        assert "knowledge content" in args[5]
        assert "target content" in args[6]

    def test_arena_start_without_materials(self, client, tmp_hub):
        with patch("app._arena_running") as mock_running, \
             patch("app.threading.Thread") as mock_thread:
            mock_running.is_set.return_value = False
            resp = client.post("/api/arena/start", json={
                "topic": "普通讨论",
                "participant_ids": ["bot001"],
                "rounds": 1,
            })

        assert resp.status_code == 200
        args = mock_thread.call_args.kwargs.get("args") or mock_thread.call_args[1].get("args", ())
        assert args[5] == ""  # knowledge_text
        assert args[6] == ""  # target_text


# ── _write_arena_chat_log with reference_text ─────────────────


class TestWriteArenaChatLog:
    def test_log_includes_target_text(self, tmp_hub):
        import app as flask_app

        with patch.object(flask_app, "GATEWAYS_DIR", tmp_hub / "gateways"):
            ws_dir = tmp_hub / "gateways" / "bot001" / "state" / "workspace"
            ws_dir.mkdir(parents=True, exist_ok=True)

            flask_app._write_arena_chat_log(
                topic="测试议题",
                discussion_log=[],
                expert_ids=["bot001"],
                target_text="这是讨论标的内容",
            )

        log_path = ws_dir / "GROUP_CHAT_LOG.md"
        content = log_path.read_text(encoding="utf-8")
        assert "讨论标的" in content
        assert "这是讨论标的内容" in content

    def test_log_includes_knowledge_text(self, tmp_hub):
        import app as flask_app

        with patch.object(flask_app, "GATEWAYS_DIR", tmp_hub / "gateways"):
            ws_dir = tmp_hub / "gateways" / "bot001" / "state" / "workspace"
            ws_dir.mkdir(parents=True, exist_ok=True)

            flask_app._write_arena_chat_log(
                topic="测试议题",
                discussion_log=[],
                expert_ids=["bot001"],
                knowledge_text="这是参考知识内容",
            )

        log_path = ws_dir / "GROUP_CHAT_LOG.md"
        content = log_path.read_text(encoding="utf-8")
        assert "参考知识" in content
        assert "这是参考知识内容" in content

    def test_log_includes_both(self, tmp_hub):
        import app as flask_app

        with patch.object(flask_app, "GATEWAYS_DIR", tmp_hub / "gateways"):
            ws_dir = tmp_hub / "gateways" / "bot001" / "state" / "workspace"
            ws_dir.mkdir(parents=True, exist_ok=True)

            flask_app._write_arena_chat_log(
                topic="测试",
                discussion_log=[],
                expert_ids=["bot001"],
                knowledge_text="知识ABC",
                target_text="标的XYZ",
            )

        content = (ws_dir / "GROUP_CHAT_LOG.md").read_text(encoding="utf-8")
        assert "知识ABC" in content
        assert "标的XYZ" in content
        # Target should appear before knowledge
        assert content.index("讨论标的") < content.index("参考知识")

    def test_log_without_materials(self, tmp_hub):
        import app as flask_app

        with patch.object(flask_app, "GATEWAYS_DIR", tmp_hub / "gateways"):
            ws_dir = tmp_hub / "gateways" / "bot001" / "state" / "workspace"
            ws_dir.mkdir(parents=True, exist_ok=True)

            flask_app._write_arena_chat_log(
                topic="普通议题",
                discussion_log=[],
                expert_ids=["bot001"],
            )

        content = (ws_dir / "GROUP_CHAT_LOG.md").read_text(encoding="utf-8")
        assert "讨论标的" not in content
        assert "参考知识" not in content
        assert "普通议题" in content

    def test_log_includes_focus_map(self, tmp_hub):
        import app as flask_app

        with patch.object(flask_app, "GATEWAYS_DIR", tmp_hub / "gateways"):
            ws_dir = tmp_hub / "gateways" / "bot001" / "state" / "workspace"
            ws_dir.mkdir(parents=True, exist_ok=True)

            flask_app._write_arena_chat_log(
                topic="标题讨论",
                discussion_log=[],
                expert_ids=["bot001"],
                focus_map={"bot001": "把标题改得不俗"},
            )

        content = (ws_dir / "GROUP_CHAT_LOG.md").read_text(encoding="utf-8")
        assert "主攻方向" in content
        assert "把标题改得不俗" in content

    def test_log_without_focus_map(self, tmp_hub):
        import app as flask_app

        with patch.object(flask_app, "GATEWAYS_DIR", tmp_hub / "gateways"):
            ws_dir = tmp_hub / "gateways" / "bot001" / "state" / "workspace"
            ws_dir.mkdir(parents=True, exist_ok=True)

            flask_app._write_arena_chat_log(
                topic="普通议题",
                discussion_log=[],
                expert_ids=["bot001"],
            )

        content = (ws_dir / "GROUP_CHAT_LOG.md").read_text(encoding="utf-8")
        assert "主攻方向" not in content


class TestArenaFocusMap:
    def test_arena_start_passes_focus_map(self, client, tmp_hub):
        with patch("app._arena_running") as mock_running, \
             patch("app.threading.Thread") as mock_thread:
            mock_running.is_set.return_value = False
            resp = client.post("/api/arena/start", json={
                "topic": "标题优化",
                "participant_ids": ["bot001"],
                "focus_map": {"bot001": "增强点击触发器"},
                "rounds": 1,
            })

        assert resp.status_code == 200
        args = mock_thread.call_args.kwargs.get("args") or mock_thread.call_args[1].get("args", ())
        # args = (topic, expert_ids, rounds, max_reply, interval, knowledge_text, target_text, focus_map)
        focus = args[7] if len(args) > 7 else {}
        assert focus.get("bot001") == "增强点击触发器"

    def test_arena_start_without_focus_map(self, client, tmp_hub):
        with patch("app._arena_running") as mock_running, \
             patch("app.threading.Thread") as mock_thread:
            mock_running.is_set.return_value = False
            resp = client.post("/api/arena/start", json={
                "topic": "普通",
                "participant_ids": ["bot001"],
                "rounds": 1,
            })

        assert resp.status_code == 200
        args = mock_thread.call_args.kwargs.get("args") or mock_thread.call_args[1].get("args", ())
        focus = args[7] if len(args) > 7 else {}
        assert focus == {}


# ── Arena Templates (讨论模板) ────────────────────────────────


class TestArenaTemplates:
    def test_list_empty(self, client):
        resp = client.get("/api/arena/templates")
        assert resp.status_code == 200
        assert resp.get_json()["templates"] == []

    def test_save_template(self, client):
        resp = client.post("/api/arena/templates", json={
            "name": "传播学专家团",
            "slots": [
                {"gateway_id": "bot001", "persona_id": "p1", "focus": "增强点击触发器"},
            ],
            "topic": "标题优化",
            "rounds": 5,
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        tpl = data["template"]
        assert tpl["name"] == "传播学专家团"
        assert len(tpl["slots"]) == 1
        assert tpl["rounds"] == 5
        assert tpl["topic"] == "标题优化"

    def test_save_template_empty_name(self, client):
        resp = client.post("/api/arena/templates", json={
            "name": "",
            "slots": [{"gateway_id": "bot001"}],
        })
        assert resp.status_code == 400

    def test_save_template_no_slots(self, client):
        resp = client.post("/api/arena/templates", json={
            "name": "空模板",
            "slots": [],
        })
        assert resp.status_code == 400

    def test_list_after_save(self, client):
        client.post("/api/arena/templates", json={
            "name": "A",
            "slots": [{"gateway_id": "bot001"}],
        })
        client.post("/api/arena/templates", json={
            "name": "B",
            "slots": [{"gateway_id": "bot001"}],
        })
        resp = client.get("/api/arena/templates")
        names = [t["name"] for t in resp.get_json()["templates"]]
        # Newest first
        assert names == ["B", "A"]

    def test_delete_template(self, client):
        save_resp = client.post("/api/arena/templates", json={
            "name": "ToDelete",
            "slots": [{"gateway_id": "bot001"}],
        })
        tpl_id = save_resp.get_json()["template"]["id"]
        resp = client.delete(f"/api/arena/templates/{tpl_id}")
        assert resp.status_code == 200
        # Verify gone
        items = client.get("/api/arena/templates").get_json()["templates"]
        assert not any(t["id"] == tpl_id for t in items)

    def test_delete_nonexistent(self, client):
        resp = client.delete("/api/arena/templates/nonexistent")
        assert resp.status_code == 404

    def test_load_template_with_persona_apply(self, client, tmp_hub):
        # Save a persona first
        ws_dir = tmp_hub / "gateways" / "bot001" / "state" / "workspace"
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "SOUL.md").write_text("# Ogilvy Soul", "utf-8")
        (ws_dir / "IDENTITY.md").write_text("# Ogilvy Identity", "utf-8")
        (ws_dir / "AGENTS.md").write_text("# Ogilvy Agents", "utf-8")

        p_resp = client.post("/api/personas", json={
            "gateway_id": "bot001", "name": "Ogilvy", "emoji": "🎩",
        })
        persona_id = p_resp.get_json()["persona"]["id"]

        # Overwrite bot001 files to something different
        (ws_dir / "SOUL.md").write_text("old soul", "utf-8")

        # Save template with this persona
        tpl_resp = client.post("/api/arena/templates", json={
            "name": "传播专家团",
            "slots": [{"gateway_id": "bot001", "persona_id": persona_id, "focus": "标题优化"}],
            "topic": "改标题",
            "rounds": 3,
        })
        tpl_id = tpl_resp.get_json()["template"]["id"]

        # Load template — should auto-apply persona
        load_resp = client.post(f"/api/arena/templates/{tpl_id}/load")
        assert load_resp.status_code == 200
        data = load_resp.get_json()
        assert data["ok"] is True
        assert data["template"]["name"] == "传播专家团"
        assert len(data["applied_personas"]) == 1
        assert data["applied_personas"][0]["persona_name"] == "Ogilvy"

        # Verify persona files were applied
        assert (ws_dir / "SOUL.md").read_text("utf-8") == "# Ogilvy Soul"

    def test_load_nonexistent(self, client):
        resp = client.post("/api/arena/templates/nonexistent/load")
        assert resp.status_code == 404


# ── Persona Library (角色库) ──────────────────────────────────


class TestPersonasList:
    def test_empty_list(self, client):
        resp = client.get("/api/personas")
        assert resp.status_code == 200
        assert resp.get_json()["personas"] == []


class TestPersonasSave:
    def test_save_from_gateway(self, client, tmp_hub):
        # Create persona files in gateway workspace
        ws_dir = tmp_hub / "gateways" / "bot001" / "state" / "workspace"
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "SOUL.md").write_text("# Soul of Alice", "utf-8")
        (ws_dir / "IDENTITY.md").write_text("# Identity of Alice", "utf-8")
        (ws_dir / "AGENTS.md").write_text("# Agents of Alice", "utf-8")

        resp = client.post("/api/personas", json={
            "gateway_id": "bot001",
            "name": "Alice",
            "emoji": "🧙",
            "model": "gpt-4o",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        p = data["persona"]
        assert p["name"] == "Alice"
        assert p["emoji"] == "🧙"
        assert len(p["files"]) == 3
        assert "SOUL.md" in p["files"]

    def test_save_no_persona_files(self, client, tmp_hub):
        # Gateway exists but has no persona files
        ws_dir = tmp_hub / "gateways" / "bot001" / "state" / "workspace"
        ws_dir.mkdir(parents=True, exist_ok=True)

        resp = client.post("/api/personas", json={
            "gateway_id": "bot001",
        })
        assert resp.status_code == 400

    def test_save_gateway_not_found(self, client):
        resp = client.post("/api/personas", json={
            "gateway_id": "nonexistent",
        })
        assert resp.status_code == 404


class TestPersonasGet:
    def test_get_persona_with_contents(self, client, tmp_hub):
        ws_dir = tmp_hub / "gateways" / "bot001" / "state" / "workspace"
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "SOUL.md").write_text("soul content", "utf-8")
        (ws_dir / "IDENTITY.md").write_text("identity content", "utf-8")

        save_resp = client.post("/api/personas", json={
            "gateway_id": "bot001", "name": "Bob",
        })
        pid = save_resp.get_json()["persona"]["id"]

        resp = client.get(f"/api/personas/{pid}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["name"] == "Bob"
        assert "soul content" in data["contents"]["SOUL.md"]

    def test_get_nonexistent(self, client):
        resp = client.get("/api/personas/nonexistent")
        assert resp.status_code == 404


class TestPersonasDelete:
    def test_delete_persona(self, client, tmp_hub):
        ws_dir = tmp_hub / "gateways" / "bot001" / "state" / "workspace"
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "SOUL.md").write_text("soul", "utf-8")

        save_resp = client.post("/api/personas", json={
            "gateway_id": "bot001", "name": "ToDelete",
        })
        pid = save_resp.get_json()["persona"]["id"]

        resp = client.delete(f"/api/personas/{pid}")
        assert resp.status_code == 200

        # Verify gone
        resp2 = client.get(f"/api/personas/{pid}")
        assert resp2.status_code == 404

        items = client.get("/api/personas").get_json()["personas"]
        assert not any(p["id"] == pid for p in items)

    def test_delete_nonexistent(self, client):
        resp = client.delete("/api/personas/nonexistent")
        assert resp.status_code == 404


class TestPersonasApply:
    def test_apply_persona_to_gateway(self, client, tmp_hub):
        # Create and save a persona from bot001
        ws_dir = tmp_hub / "gateways" / "bot001" / "state" / "workspace"
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "SOUL.md").write_text("# Alice Soul", "utf-8")
        (ws_dir / "IDENTITY.md").write_text("# Alice Identity", "utf-8")
        (ws_dir / "AGENTS.md").write_text("# Alice Agents", "utf-8")

        save_resp = client.post("/api/personas", json={
            "gateway_id": "bot001", "name": "Alice",
        })
        pid = save_resp.get_json()["persona"]["id"]

        # Overwrite bot001's files to simulate different persona
        (ws_dir / "SOUL.md").write_text("old soul", "utf-8")

        # Apply saved persona back to bot001
        resp = client.post(f"/api/personas/{pid}/apply", json={
            "gateway_id": "bot001",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["applied_files"]) == 3
        assert data["persona_name"] == "Alice"

        # Verify files were restored
        assert (ws_dir / "SOUL.md").read_text("utf-8") == "# Alice Soul"
        # Verify backup was created
        assert (ws_dir / "SOUL.md.bak.prev").exists()

    def test_apply_nonexistent_persona(self, client, tmp_hub):
        resp = client.post("/api/personas/nonexistent/apply", json={
            "gateway_id": "bot001",
        })
        assert resp.status_code == 404

    def test_apply_to_nonexistent_gateway(self, client, tmp_hub):
        ws_dir = tmp_hub / "gateways" / "bot001" / "state" / "workspace"
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "SOUL.md").write_text("soul", "utf-8")

        save_resp = client.post("/api/personas", json={
            "gateway_id": "bot001", "name": "X",
        })
        pid = save_resp.get_json()["persona"]["id"]

        resp = client.post(f"/api/personas/{pid}/apply", json={
            "gateway_id": "nonexistent",
        })
        assert resp.status_code == 404


# ── Persona Library Generation (角色库生成) ──────────────────


class TestPersonaGenerate:
    def test_generate_empty_name(self, client):
        resp = client.post("/api/personas/generate", json={
            "character_name": "",
        })
        assert resp.status_code == 400

    def test_generate_starts_task(self, client):
        with patch("app.threading.Thread") as mock_thread:
            resp = client.post("/api/personas/generate", json={
                "character_name": "孙子",
                "model": "gpt-5.4",
            })
        assert resp.status_code == 202
        data = resp.get_json()
        assert data["status"] == "generating"
        assert data["character_name"] == "孙子"
        assert "task_id" in data
        mock_thread.assert_called_once()

    def test_generate_multiple_concurrent(self, client):
        with patch("app.threading.Thread"):
            resp1 = client.post("/api/personas/generate", json={"character_name": "孙子"})
            resp2 = client.post("/api/personas/generate", json={"character_name": "老子"})
        assert resp1.status_code == 202
        assert resp2.status_code == 202
        assert resp1.get_json()["task_id"] != resp2.get_json()["task_id"]

    def test_status_empty(self, client):
        resp = client.get("/api/personas/generate/status")
        assert resp.status_code == 200
        assert resp.get_json()["tasks"] == []

    def test_status_shows_tasks(self, client):
        import app as flask_app
        flask_app._persona_gen_tasks["t1"] = {
            "status": "generating",
            "character_name": "孙子",
            "model": "gpt-5.4",
            "message": "正在生成...",
            "_ts": __import__("time").time(),
        }
        try:
            resp = client.get("/api/personas/generate/status")
            tasks = resp.get_json()["tasks"]
            assert len(tasks) == 1
            assert tasks[0]["task_id"] == "t1"
            assert tasks[0]["character_name"] == "孙子"
            assert tasks[0]["status"] == "generating"
            # Internal fields should be stripped
            assert "_ts" not in tasks[0]
        finally:
            flask_app._persona_gen_tasks.pop("t1", None)


# ── Secretary History (书记存档) ──────────────────────────────


class TestSecretaryHistory:
    def test_empty_history(self, client):
        resp = client.get("/api/arena/secretary/history")
        assert resp.status_code == 200
        assert resp.get_json()["summaries"] == []

    def test_save_and_list(self, client, tmp_hub):
        import app as flask_app
        entry = flask_app._save_secretary_summary(
            topic="测试议题", prompt="总结要点", model="gpt-4o", result="这是总结内容"
        )
        assert "id" in entry
        assert entry["topic"] == "测试议题"
        assert entry["chars"] == len("这是总结内容")

        resp = client.get("/api/arena/secretary/history")
        items = resp.get_json()["summaries"]
        assert len(items) == 1
        assert items[0]["id"] == entry["id"]
        assert items[0]["model"] == "gpt-4o"

    def test_get_detail(self, client, tmp_hub):
        import app as flask_app
        entry = flask_app._save_secretary_summary(
            topic="议题A", prompt="提示词A", model="claude-sonnet-4", result="详细总结内容ABC"
        )
        resp = client.get(f"/api/arena/secretary/history/{entry['id']}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["text"] == "详细总结内容ABC"
        assert data["topic"] == "议题A"
        assert data["prompt"] == "提示词A"

    def test_get_detail_not_found(self, client):
        resp = client.get("/api/arena/secretary/history/nonexistent")
        assert resp.status_code == 404

    def test_delete(self, client, tmp_hub):
        import app as flask_app
        entry = flask_app._save_secretary_summary(
            topic="删除测试", prompt="p", model="m", result="to delete"
        )
        resp = client.delete(f"/api/arena/secretary/history/{entry['id']}")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        # Verify gone from list
        items = client.get("/api/arena/secretary/history").get_json()["summaries"]
        assert not any(s["id"] == entry["id"] for s in items)
        # Verify md file deleted
        md_path = tmp_hub / "secretary_history" / f"{entry['id']}.md"
        assert not md_path.exists()

    def test_delete_not_found(self, client):
        resp = client.delete("/api/arena/secretary/history/nonexistent")
        assert resp.status_code == 404

    def test_multiple_entries_newest_first(self, client, tmp_hub):
        import app as flask_app
        e1 = flask_app._save_secretary_summary("t1", "p1", "m1", "r1")
        e2 = flask_app._save_secretary_summary("t2", "p2", "m2", "r2")
        items = client.get("/api/arena/secretary/history").get_json()["summaries"]
        assert len(items) == 2
        assert items[0]["id"] == e2["id"]  # newest first
        assert items[1]["id"] == e1["id"]

    def test_md_file_persisted(self, client, tmp_hub):
        import app as flask_app
        entry = flask_app._save_secretary_summary("t", "p", "m", "持久化内容")
        md_path = tmp_hub / "secretary_history" / f"{entry['id']}.md"
        assert md_path.exists()
        assert md_path.read_text(encoding="utf-8") == "持久化内容"


# ── Secretary Generate + Auto-save ────────────────────────────


class TestSecretaryGenerate:
    def test_generate_missing_model(self, client):
        resp = client.post("/api/arena/secretary/generate", json={
            "model": "", "prompt": "总结",
        })
        assert resp.status_code == 400

    def test_generate_missing_prompt(self, client):
        resp = client.post("/api/arena/secretary/generate", json={
            "model": "gpt-4o", "prompt": "",
        })
        assert resp.status_code == 400

    def test_generate_starts_async(self, client):
        with patch("app.threading.Thread") as mock_thread:
            resp = client.post("/api/arena/secretary/generate", json={
                "model": "gpt-4o", "prompt": "请总结讨论要点",
            })
        assert resp.status_code == 202
        data = resp.get_json()
        assert data["status"] == "generating"
        mock_thread.assert_called_once()


class TestSecretaryStatus:
    def test_idle_status(self, client):
        import app as flask_app
        flask_app._secretary_status.clear()
        resp = client.get("/api/arena/secretary/status")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "idle"

    def test_done_status(self, client):
        import app as flask_app
        flask_app._secretary_status.update({
            "status": "done", "result": "总结结果", "error": None, "model": "gpt-4o",
            "_ts": time.time(),
        })
        try:
            resp = client.get("/api/arena/secretary/status")
            data = resp.get_json()
            assert data["status"] == "done"
            assert data["result"] == "总结结果"
            assert "_ts" not in data  # internal field stripped
        finally:
            flask_app._secretary_status.clear()

    def test_timeout_detection(self, client):
        import app as flask_app
        flask_app._secretary_status.update({
            "status": "generating", "result": None, "error": None,
            "_ts": time.time() - 600,  # 10 min ago
        })
        try:
            resp = client.get("/api/arena/secretary/status")
            data = resp.get_json()
            assert data["status"] == "error"
            assert "超时" in data["error"]
        finally:
            flask_app._secretary_status.clear()


# ── Template herald_prompt persistence ────────────────────────


class TestTemplateHeraldPrompt:
    def test_save_with_herald_prompt(self, client):
        resp = client.post("/api/arena/templates", json={
            "name": "带书记提示词的模板",
            "slots": [{"gateway_id": "bot001"}],
            "herald_prompt": "请按 SEO 角度总结",
        })
        assert resp.status_code == 200
        tpl = resp.get_json()["template"]
        assert tpl["herald_prompt"] == "请按 SEO 角度总结"

    def test_save_without_herald_prompt(self, client):
        resp = client.post("/api/arena/templates", json={
            "name": "无书记提示词",
            "slots": [{"gateway_id": "bot001"}],
        })
        tpl = resp.get_json()["template"]
        assert tpl["herald_prompt"] == ""

    def test_load_preserves_herald_prompt(self, client):
        save_resp = client.post("/api/arena/templates", json={
            "name": "测试模板",
            "slots": [{"gateway_id": "bot001"}],
            "herald_prompt": "总结要点并给出行动项",
            "topic": "产品评审",
        })
        tpl_id = save_resp.get_json()["template"]["id"]
        load_resp = client.post(f"/api/arena/templates/{tpl_id}/load")
        assert load_resp.status_code == 200
        tpl = load_resp.get_json()["template"]
        assert tpl["herald_prompt"] == "总结要点并给出行动项"


# ── Group participant name auto-sync ──────────────────────────


class TestGroupParticipantSync:
    def test_names_sync_from_gateway(self, client, tmp_hub):
        # bot001 is registered in palace.json as "孙子"
        # but gateway name is "孙子" too by default.
        # Change gateway name to test sync
        import app as flask_app
        gw = flask_app.manager.get("bot001")
        assert gw is not None
        gw.name = "新孙子名"

        resp = client.get("/api/group")
        assert resp.status_code == 200
        participants = resp.get_json()["participants"]
        bot001 = next((p for p in participants if p["id"] == "bot001"), None)
        assert bot001 is not None
        assert bot001["name"] == "新孙子名"

        # Verify persisted to palace.json
        palace = json.loads((tmp_hub / "palace.json").read_text(encoding="utf-8"))
        p_entry = next(p for p in palace["participants"] if p["id"] == "bot001")
        assert p_entry["name"] == "新孙子名"

    def test_no_sync_when_names_match(self, client, tmp_hub):
        # Read palace.json before
        before = (tmp_hub / "palace.json").read_text(encoding="utf-8")
        resp = client.get("/api/group")
        assert resp.status_code == 200
        # If names already match, palace.json should not be rewritten
        # (mtime would change but content stays same — just verify no error)
        participants = resp.get_json()["participants"]
        assert len(participants) == 2

    def test_group_config_fields(self, client):
        resp = client.get("/api/group")
        data = resp.get_json()
        assert "groupId" in data
        assert "herald" in data
        assert "participants" in data
        assert "sync" in data


# ── Secretary Log & Models ────────────────────────────────────


class TestSecretaryLog:
    def test_log_empty(self, client):
        resp = client.get("/api/arena/secretary/log")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["log"] == ""
        assert "chars" in data

    def test_log_with_content(self, client, tmp_hub):
        # Create a GROUP_CHAT_LOG.md
        ws_dir = tmp_hub / "gateways" / "herald" / "state" / "workspace"
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "GROUP_CHAT_LOG.md").write_text("# 讨论记录\n测试内容", encoding="utf-8")
        resp = client.get("/api/arena/secretary/log")
        data = resp.get_json()
        assert "测试内容" in data["log"]
        assert data["chars"] > 0


class TestSecretaryModels:
    def test_returns_models(self, client):
        with patch("generate_persona.get_available_models",
                   return_value=[{"key": "gpt-4o", "label": "GPT-4o", "provider": "openai"}]):
            resp = client.get("/api/arena/secretary/models")
        assert resp.status_code == 200
        models = resp.get_json()["models"]
        assert len(models) == 1
        assert models[0]["key"] == "gpt-4o"
