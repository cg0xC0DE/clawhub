from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from gateway_manager import GatewayManager, HUB_DIR, GATEWAYS_DIR, provision_gateway
from group_sync import start_sync, stop_sync, sync_once, _load_palace
from eunuch import start_eunuch, stop_eunuch, query_ext_message, submit_actions as eunuch_submit, ledger_all, get_pool as eunuch_get_pool, collect_once as eunuch_collect_once
from compact_session import compact_gateway_sessions
import os
import json
import threading
import urllib.request
import urllib.error
import uuid
import time as _time_mod
from pathlib import Path

app = Flask(__name__, static_folder=str(HUB_DIR / "frontend"), static_url_path="")
CORS(app)

manager = GatewayManager()


# ── Static frontend ────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ── Gateway list & CRUD ────────────────────────────────────────
@app.route("/api/gateways", methods=["GET"])
def list_gateways():
    manager.reload()
    return jsonify([gw.to_dict() for gw in manager.list_all()])


@app.route("/api/validate-character", methods=["POST"])
def validate_character():
    """Validate a character name for gateway creation.
    Extracts the character portion from input like '孔子 opus 4.6'.
    Returns { valid, emoji, reason, character_name }.
    """
    data = request.json or {}
    raw = (data.get("name") or "").strip()
    if not raw:
        return jsonify({"valid": False, "emoji": "", "reason": "名称不能为空"})

    # Known characters → emoji mapping
    _EMOJI_HINTS = {
        "孔子": "📜", "老子": "☯️", "孙子": "⚔️", "庄子": "🦋",
        "孟子": "📖", "韩非": "⚖️", "墨子": "🛡️", "荀子": "📚",
        "鬼谷子": "🌀", "商鞅": "📏", "管仲": "🏛️", "吴起": "🗡️",
        "诸葛亮": "🪶", "曹操": "🗡️", "刘备": "👑", "关羽": "🐉",
        "司马懿": "🦊", "周瑜": "🔥", "赵云": "🛡️", "张飞": "⚡",
        "李白": "🍶", "杜甫": "🏔️", "苏轼": "🌊", "王阳明": "💡",
        "朱熹": "📖", "陆游": "⚔️", "辛弃疾": "🏹", "李清照": "🌸",
        "牛顿": "🍎", "爱因斯坦": "⚛️", "达芬奇": "🎨", "亚里士多德": "🏛️",
        "苏格拉底": "🏺", "柏拉图": "📐", "马基雅维利": "🦊",
        "尼采": "⚡", "康德": "📏", "黑格尔": "🌀", "马克思": "🔨",
        "孙中山": "🌅", "鲁迅": "🖊️", "胡适": "📰",
    }

    # Try exact match first, then substring match (longest first)
    extracted = None
    for known in sorted(_EMOJI_HINTS.keys(), key=len, reverse=True):
        if known in raw:
            extracted = known
            break

    if extracted:
        emoji = _EMOJI_HINTS[extracted]
        suffix = raw.replace(extracted, "").strip()
        note = f"（附注: {suffix}）" if suffix else ""
        return jsonify({
            "valid": True, "emoji": emoji,
            "reason": f"已识别「{extracted}」{note}",
            "character_name": extracted,
        })

    # No known match — still valid, persona generation will handle it
    return jsonify({
        "valid": True, "emoji": "🪭",
        "reason": f"将为「{raw}」生成专家人格",
        "character_name": raw,
    })


@app.route("/api/gateways", methods=["POST"])
def add_gateway():
    data = request.json or {}
    required = ["id", "name", "port"]
    for f in required:
        if not data.get(f):
            return jsonify({"error": f"'{f}' is required"}), 400

    gw_id = data["id"].strip().lower().replace(" ", "_")
    if manager.get(gw_id):
        return jsonify({"error": f"Gateway '{gw_id}' already exists"}), 409

    port = int(data["port"])
    name = data["name"]
    emoji = data.get("emoji", "🪭")
    primary_model = data.get("primary_model", "")
    character_name = data.get("character_name", "").strip()

    try:
        provision_gateway(gw_id, name, emoji, port, primary_model)
    except Exception as e:
        return jsonify({"error": f"Provisioning failed: {e}"}), 500

    # If a historical character name is provided, generate persona in background
    if character_name:
        def _gen_persona():
            try:
                from generate_persona import generate_persona
                generate_persona(gw_id, character_name)
            except Exception as e:
                print(f"[persona] generation failed for {gw_id}: {e}")

        threading.Thread(target=_gen_persona, daemon=True).start()

    gw_def = {
        "id": gw_id,
        "name": name,
        "emoji": emoji,
        "port": port,
        "config_file": f"gateways/{gw_id}/openclaw.json",
        "workspace_dir": f"gateways/{gw_id}/state/workspace",
        "state_dir": f"gateways/{gw_id}/state",
        "editable_files": [
            f"gateways/{gw_id}/state/workspace/SOUL.md",
            f"gateways/{gw_id}/state/workspace/IDENTITY.md",
            f"gateways/{gw_id}/state/workspace/MEMORY.md",
            f"gateways/{gw_id}/state/workspace/AGENTS.md",
            f"gateways/{gw_id}/state/workspace/HEARTBEAT.md",
            f"gateways/{gw_id}/openclaw.json",
        ],
    }
    gw = manager.add(gw_def)
    return jsonify(gw.to_dict()), 201


@app.route("/api/persona-models", methods=["GET"])
def list_persona_models():
    """Return available LLM models for persona generation."""
    from generate_persona import get_available_models
    return jsonify(get_available_models())


_persona_gen_status: dict[str, dict] = {}  # gw_id -> {status, message, files, error, _ts}
_persona_gen_tasks: dict[str, dict] = {}  # task_id -> {status, character_name, model, ...}
_PERSONA_GEN_TIMEOUT = 300  # 5 minutes max


def _update_gateway_name(gw_id: str, new_name: str):
    """Update the gateway's display name in gateways.json and in-memory."""
    try:
        gw = manager.get(gw_id)
        if gw:
            gw.name = new_name
        gw_file = HUB_DIR / "gateways.json"
        if gw_file.exists():
            data = json.loads(gw_file.read_text(encoding="utf-8"))
            for entry in data:
                if entry.get("id") == gw_id:
                    entry["name"] = new_name
                    break
            gw_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"[persona_gen] Updated gateway name: {gw_id} -> {new_name}")
    except Exception as e:
        print(f"[persona_gen] Failed to update gateway name: {e}")


@app.route("/api/gateways/<gw_id>/regenerate-persona", methods=["POST"])
def regenerate_persona_endpoint(gw_id):
    """Regenerate persona files for a gateway using specified model (async)."""
    import time as _time
    gw = manager.get(gw_id)
    if not gw:
        return jsonify({"error": "Not found"}), 404

    # Check if already generating — but auto-clear stale status (> 5 min)
    existing = _persona_gen_status.get(gw_id, {})
    if existing.get("status") == "generating":
        elapsed = _time.time() - existing.get("_ts", 0)
        if elapsed < _PERSONA_GEN_TIMEOUT:
            return jsonify({"status": "generating", "message": f"已在生成中（{int(elapsed)}s）..."}), 202
        else:
            print(f"[persona_gen] Stale 'generating' status for {gw_id} ({elapsed:.0f}s), clearing")
            _persona_gen_status[gw_id] = {"status": "error", "error": "上次生成超时，已自动清除"}

    data = request.json or {}
    character_name = data.get("character_name", "").strip()
    model = data.get("model", "gpt-4o")

    if not character_name:
        character_name = gw.name or gw_id

    _persona_gen_status[gw_id] = {
        "status": "generating",
        "message": f"正在用 {model} 为 {character_name} 生成...",
        "_ts": _time.time(),
    }

    def _bg_generate():
        import time as _t
        start = _t.time()
        from generate_persona import generate_persona as gen_persona
        try:
            print(f"[persona_gen] BG started: {gw_id} ({character_name}) model={model}")
            files = gen_persona(gw_id, character_name, model=model, force=True)
            elapsed = _t.time() - start
            if not files:
                msg = f"LLM ({model}) 未返回可解析的内容（缺少 ===SOUL.MD=== 等分隔符）"
                print(f"[persona_gen] BG {gw_id}: EMPTY result after {elapsed:.1f}s")
                _persona_gen_status[gw_id] = {"status": "error", "error": msg}
            else:
                print(f"[persona_gen] BG {gw_id}: SUCCESS {list(files.keys())} in {elapsed:.1f}s")
                _update_gateway_name(gw_id, character_name)
                # Auto-save to persona library
                gw_obj = manager.get(gw_id)
                gw_emoji = getattr(gw_obj, "emoji", "🪭") if gw_obj else "🪭"
                try:
                    _save_persona_to_library(character_name, gw_emoji, model, gw_id, files)
                except Exception as e:
                    print(f"[persona_lib] Auto-save failed: {e}")
                _persona_gen_status[gw_id] = {
                    "status": "done",
                    "message": f"已用 {model} 重新生成 {len(files)} 个文件（{elapsed:.0f}秒），已自动存入角色库",
                    "files": {k: len(v) for k, v in files.items()},
                }
        except BaseException as e:
            elapsed = _t.time() - start
            err_msg = str(e)[:300]
            print(f"[persona_gen] BG {gw_id}: ERROR after {elapsed:.1f}s: {e}")
            _persona_gen_status[gw_id] = {"status": "error", "error": f"[{model}] {err_msg}"}

    threading.Thread(target=_bg_generate, daemon=True, name=f"persona-gen-{gw_id}").start()
    return jsonify({"status": "generating", "message": f"正在用 {model} 生成..."}), 202


@app.route("/api/gateways/<gw_id>/regenerate-persona/status", methods=["GET"])
def regenerate_persona_status(gw_id):
    """Poll persona generation status. Also auto-clears stale 'generating' status."""
    import time as _time
    info = _persona_gen_status.get(gw_id)
    if not info:
        return jsonify({"status": "idle"})
    # Auto-clear stale generating status
    if info.get("status") == "generating":
        elapsed = _time.time() - info.get("_ts", 0)
        if elapsed > _PERSONA_GEN_TIMEOUT:
            info = {"status": "error", "error": f"生成超时（{int(elapsed)}秒），请重试"}
            _persona_gen_status[gw_id] = info
    # Strip internal fields
    return jsonify({k: v for k, v in info.items() if not k.startswith("_")})


@app.route("/api/gateways/<gw_id>", methods=["GET"])
def get_gateway(gw_id):
    gw = manager.get(gw_id)
    if not gw:
        return jsonify({"error": "Not found"}), 404
    return jsonify(gw.to_dict())


@app.route("/api/gateways/<gw_id>", methods=["PATCH"])
def update_gateway(gw_id):
    gw = manager.get(gw_id)
    if not gw:
        return jsonify({"error": "Not found"}), 404
    data = request.json or {}
    allowed = ["name", "emoji", "port", "config_file", "workspace_dir", "state_dir", "editable_files"]
    fields = {k: v for k, v in data.items() if k in allowed}
    gw = manager.update(gw_id, fields)
    return jsonify(gw.to_dict())


@app.route("/api/gateways/<gw_id>", methods=["DELETE"])
def remove_gateway(gw_id):
    if not manager.remove(gw_id):
        return jsonify({"error": "Not found"}), 404

    cleanup = request.args.get("cleanup", "false").lower() in ("true", "1", "yes")
    cleaned = []
    if cleanup:
        from gateway_manager import cleanup_gateway_files
        cleaned = cleanup_gateway_files(gw_id)

    return jsonify({"success": True, "cleaned": cleaned})


# ── Process control ────────────────────────────────────────────
@app.route("/api/gateways/<gw_id>/start", methods=["POST"])
def start_gateway(gw_id):
    gw = manager.get(gw_id)
    if not gw:
        return jsonify({"error": "Not found"}), 404
    ok, msg = gw.start()
    if ok:
        return jsonify(gw.to_dict())
    return jsonify({"error": msg}), 400


@app.route("/api/gateways/<gw_id>/stop", methods=["POST"])
def stop_gateway(gw_id):
    gw = manager.get(gw_id)
    if not gw:
        return jsonify({"error": "Not found"}), 404
    ok, msg = gw.stop()
    return jsonify({**gw.to_dict(), "message": msg})


@app.route("/api/gateways/<gw_id>/restart", methods=["POST"])
def restart_gateway(gw_id):
    gw = manager.get(gw_id)
    if not gw:
        return jsonify({"error": "Not found"}), 404
    ok, msg = gw.restart()
    if ok:
        return jsonify(gw.to_dict())
    return jsonify({"error": msg}), 400


# ── Logs ───────────────────────────────────────────────────────
@app.route("/api/gateways/<gw_id>/logs", methods=["GET"])
def get_logs(gw_id):
    gw = manager.get(gw_id)
    if not gw:
        return jsonify({"error": "Not found"}), 404
    offset = request.args.get("offset", 0, type=int)
    lines, total, pruned = gw.get_logs(offset)
    return jsonify({"lines": lines, "offset": total, "total": total, "pruned": pruned})


# ── File editor ────────────────────────────────────────────────
@app.route("/api/gateways/<gw_id>/files", methods=["GET"])
def list_files(gw_id):
    gw = manager.get(gw_id)
    if not gw:
        return jsonify({"error": "Not found"}), 404
    result = []
    for rel in gw.editable_files:
        abs_path = HUB_DIR / rel
        result.append({
            "rel": rel,
            "name": abs_path.name,
            "exists": abs_path.exists(),
            "size": abs_path.stat().st_size if abs_path.exists() else 0,
        })
    return jsonify(result)


@app.route("/api/gateways/<gw_id>/files/read", methods=["GET"])
def read_file(gw_id):
    gw = manager.get(gw_id)
    if not gw:
        return jsonify({"error": "Not found"}), 404
    rel = request.args.get("path", "")
    if not rel:
        return jsonify({"error": "path required"}), 400
    # Security: must be in editable_files list
    if rel not in gw.editable_files:
        return jsonify({"error": "File not in editable list"}), 403
    abs_path = HUB_DIR / rel
    if not abs_path.exists():
        return jsonify({"content": "", "exists": False, "path": rel})
    try:
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                content = abs_path.read_text(encoding=enc)
                if content and ord(content[0]) == 0xFEFF:
                    content = content[1:]
                return jsonify({"content": content, "exists": True, "path": rel})
            except UnicodeDecodeError:
                continue
        return jsonify({"error": "Cannot decode file"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/gateways/<gw_id>/files/write", methods=["PUT"])
def write_file(gw_id):
    gw = manager.get(gw_id)
    if not gw:
        return jsonify({"error": "Not found"}), 404
    data = request.json or {}
    rel = data.get("path", "")
    content = data.get("content", "")
    if not rel:
        return jsonify({"error": "path required"}), 400
    if rel not in gw.editable_files:
        return jsonify({"error": "File not in editable list"}), 403
    abs_path = HUB_DIR / rel
    try:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")
        return jsonify({"success": True, "path": rel})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Token management ───────────────────────────────────────────
@app.route("/api/gateways/<gw_id>/token", methods=["PUT"])
def set_token(gw_id):
    gw = manager.get(gw_id)
    if not gw:
        return jsonify({"error": "Not found"}), 404
    token = (request.json or {}).get("token", "")
    if not token:
        return jsonify({"error": "token required"}), 400
    cfg_path = HUB_DIR / gw.config_file
    if not cfg_path.exists():
        return jsonify({"error": f"Config not found: {gw.config_file}"}), 404
    try:
        raw = cfg_path.read_text(encoding="utf-8")
        if raw and ord(raw[0]) == 0xFEFF:
            raw = raw[1:]
        cfg = json.loads(raw)
        cfg.setdefault("channels", {}).setdefault("telegram", {})["botToken"] = token
        cfg_path.write_text(json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Heartbeat config ──────────────────────────────────────────
@app.route("/api/gateways/<gw_id>/heartbeat", methods=["GET"])
def get_heartbeat(gw_id):
    gw = manager.get(gw_id)
    if not gw:
        return jsonify({"error": "Not found"}), 404
    cfg_path = HUB_DIR / gw.config_file
    cfg = _read_cfg(cfg_path)
    hb = cfg.get("agents", {}).get("defaults", {}).get("heartbeat", {})
    return jsonify(hb)


@app.route("/api/gateways/<gw_id>/heartbeat", methods=["PUT"])
def set_heartbeat(gw_id):
    gw = manager.get(gw_id)
    if not gw:
        return jsonify({"error": "Not found"}), 404
    cfg_path = HUB_DIR / gw.config_file
    if not cfg_path.exists():
        return jsonify({"error": "Config not found"}), 404
    hb_data = request.json or {}
    try:
        raw = cfg_path.read_text(encoding="utf-8")
        if raw and ord(raw[0]) == 0xFEFF:
            raw = raw[1:]
        cfg = json.loads(raw)
        cfg.setdefault("agents", {}).setdefault("defaults", {})["heartbeat"] = hb_data
        cfg_path.write_text(json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _read_cfg(path):
    try:
        raw = path.read_text(encoding="utf-8")
        if raw and ord(raw[0]) == 0xFEFF:
            raw = raw[1:]
        return json.loads(raw)
    except Exception:
        return {}


def _write_cfg(path, cfg):
    from pathlib import Path
    Path(path).write_text(json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8")


# ── LLM Profiles management ──────────────────────────────────

LLM_PROFILES_FILE = HUB_DIR / "llm_profiles.json"


def _read_llm_profiles() -> dict:
    if LLM_PROFILES_FILE.exists():
        return _read_cfg(LLM_PROFILES_FILE)
    return {"providers": []}


def _save_llm_profiles(data: dict):
    _write_cfg(LLM_PROFILES_FILE, data)


def _seed_llm_profiles():
    """Auto-seed API keys from existing auth-profiles.json into llm_profiles.json."""
    data = _read_llm_profiles()
    providers = {p["id"]: p for p in data.get("providers", [])}

    # Find an existing auth-profiles.json with real keys
    for gw_dir in GATEWAYS_DIR.iterdir():
        auth_path = gw_dir / "state" / "agents" / "main" / "agent" / "auth-profiles.json"
        if not auth_path.exists():
            continue
        try:
            auth_data = _read_cfg(auth_path)
            for pid, prof in auth_data.get("profiles", {}).items():
                prov_id = prof.get("provider", "")
                key = prof.get("key", "")
                if prov_id and key and prov_id in providers:
                    if not providers[prov_id].get("api_key"):
                        providers[prov_id]["api_key"] = key
        except Exception:
            continue

    data["providers"] = list(providers.values())
    _save_llm_profiles(data)
    return data


@app.route("/api/llm-profiles", methods=["GET"])
def list_llm_profiles():
    data = _read_llm_profiles()
    # Auto-seed on first access if keys are empty
    providers = data.get("providers", [])
    if providers and all(not p.get("api_key") for p in providers):
        data = _seed_llm_profiles()
    # Mask API keys for frontend display
    safe = []
    for p in data.get("providers", []):
        sp = {**p}
        key = sp.get("api_key", "")
        sp["api_key_masked"] = (key[:8] + "..." + key[-4:]) if len(key) > 12 else ("***" if key else "")
        sp["api_key_set"] = bool(key)
        del sp["api_key"]
        safe.append(sp)
    return jsonify({"providers": safe})


@app.route("/api/llm-profiles/raw", methods=["GET"])
def list_llm_profiles_raw():
    """Return full profiles including keys (for internal use)."""
    return jsonify(_read_llm_profiles())


@app.route("/api/llm-profiles/providers", methods=["POST"])
def add_or_update_provider():
    """Add or update an LLM provider."""
    data = request.json or {}
    prov_id = data.get("id", "").strip()
    if not prov_id:
        return jsonify({"error": "Provider id required"}), 400

    profiles = _read_llm_profiles()
    providers = profiles.get("providers", [])
    existing = next((p for p in providers if p["id"] == prov_id), None)

    if existing:
        for k in ("label", "api_key", "auth_mode", "base_url", "api_format", "models"):
            if k in data:
                existing[k] = data[k]
    else:
        providers.append({
            "id": prov_id,
            "label": data.get("label", prov_id),
            "api_key": data.get("api_key", ""),
            "auth_mode": data.get("auth_mode", "api_key"),
            "base_url": data.get("base_url", ""),
            "api_format": data.get("api_format", ""),
            "models": data.get("models", []),
        })

    profiles["providers"] = providers
    _save_llm_profiles(profiles)
    return jsonify({"success": True, "provider_id": prov_id})


def _sync_remove_models_from_gateways(removed_refs: set):
    """Remove model references from all gateway configs.

    removed_refs: set of 'provider/model_id' strings to remove.
    Cleans: openclaw.json (primary, fallbacks, models allowlist)
            and models.json (provider model entries).
    """
    if not removed_refs:
        return
    updated = []
    for gw_dir in GATEWAYS_DIR.iterdir():
        if not gw_dir.is_dir() or gw_dir.name.startswith("_"):
            continue
        cfg_path = gw_dir / "openclaw.json"
        if not cfg_path.exists():
            continue
        try:
            cfg = _read_cfg(cfg_path)
            changed = False
            model_section = cfg.get("agents", {}).get("defaults", {}).get("model", {})

            # Clean primary — if removed, pick first remaining fallback
            primary = model_section.get("primary", "")
            if primary in removed_refs:
                fallbacks = [f for f in model_section.get("fallbacks", []) if f not in removed_refs]
                model_section["primary"] = fallbacks[0] if fallbacks else ""
                model_section["fallbacks"] = fallbacks[1:] if fallbacks else []
                changed = True
            else:
                # Clean fallbacks
                old_fb = model_section.get("fallbacks", [])
                new_fb = [f for f in old_fb if f not in removed_refs]
                if len(new_fb) != len(old_fb):
                    model_section["fallbacks"] = new_fb
                    changed = True

            # Clean models allowlist
            models_map = cfg.get("agents", {}).get("defaults", {}).get("models", {})
            if models_map:
                for ref in removed_refs:
                    if ref in models_map:
                        del models_map[ref]
                        changed = True

            if changed:
                _write_cfg(cfg_path, cfg)
                updated.append(gw_dir.name)

            # Clean models.json (custom provider model entries)
            mj_path = gw_dir / "state" / "agents" / "main" / "agent" / "models.json"
            if mj_path.exists():
                mj = _read_cfg(mj_path)
                mj_changed = False
                for ref in removed_refs:
                    if "/" not in ref:
                        continue
                    prov_id, mid = ref.split("/", 1)
                    prov_data = mj.get("providers", {}).get(prov_id)
                    if prov_data and isinstance(prov_data.get("models"), list):
                        before = len(prov_data["models"])
                        prov_data["models"] = [m for m in prov_data["models"]
                                               if (m.get("id", "") if isinstance(m, dict) else m) != mid]
                        if len(prov_data["models"]) != before:
                            mj_changed = True
                if mj_changed:
                    _write_cfg(mj_path, mj)
        except Exception as e:
            print(f"[sync-remove-models] {gw_dir.name}: {e}")
    if updated:
        print(f"[sync-remove-models] Cleaned {removed_refs} from gateways: {updated}")


def _sync_remove_provider_from_gateways(prov_id: str):
    """Remove all references to a provider from all gateway configs.

    Cleans: auth-profiles.json (auth entry),
            openclaw.json (auth profile, model refs, custom provider),
            models.json (provider entry).
    """
    updated = []
    for gw_dir in GATEWAYS_DIR.iterdir():
        if not gw_dir.is_dir() or gw_dir.name.startswith("_"):
            continue
        changed_any = False

        # Clean auth-profiles.json
        auth_path = gw_dir / "state" / "agents" / "main" / "agent" / "auth-profiles.json"
        if auth_path.exists():
            try:
                auth_data = _read_cfg(auth_path)
                auth_key = f"{prov_id}:default"
                if auth_key in auth_data.get("profiles", {}):
                    del auth_data["profiles"][auth_key]
                    _write_cfg(auth_path, auth_data)
                    changed_any = True
            except Exception:
                pass

        # Clean openclaw.json
        cfg_path = gw_dir / "openclaw.json"
        if cfg_path.exists():
            try:
                cfg = _read_cfg(cfg_path)
                changed = False

                # Remove auth profile
                auth_profs = cfg.get("auth", {}).get("profiles", {})
                auth_key = f"{prov_id}:default"
                if auth_key in auth_profs:
                    del auth_profs[auth_key]
                    changed = True

                # Remove model refs with this provider prefix
                model_section = cfg.get("agents", {}).get("defaults", {}).get("model", {})
                primary = model_section.get("primary", "")
                if primary.startswith(f"{prov_id}/"):
                    fallbacks = [f for f in model_section.get("fallbacks", []) if not f.startswith(f"{prov_id}/")]
                    model_section["primary"] = fallbacks[0] if fallbacks else ""
                    model_section["fallbacks"] = fallbacks[1:] if fallbacks else []
                    changed = True
                else:
                    old_fb = model_section.get("fallbacks", [])
                    new_fb = [f for f in old_fb if not f.startswith(f"{prov_id}/")]
                    if len(new_fb) != len(old_fb):
                        model_section["fallbacks"] = new_fb
                        changed = True

                # Remove from models allowlist
                models_map = cfg.get("agents", {}).get("defaults", {}).get("models", {})
                if models_map:
                    to_del = [k for k in models_map if k.startswith(f"{prov_id}/")]
                    for k in to_del:
                        del models_map[k]
                        changed = True

                # Remove custom provider
                providers_section = cfg.get("models", {}).get("providers", {})
                if prov_id in providers_section:
                    del providers_section[prov_id]
                    changed = True

                if changed:
                    _write_cfg(cfg_path, cfg)
                    changed_any = True
            except Exception:
                pass

        # Clean models.json
        mj_path = gw_dir / "state" / "agents" / "main" / "agent" / "models.json"
        if mj_path.exists():
            try:
                mj = _read_cfg(mj_path)
                if prov_id in mj.get("providers", {}):
                    del mj["providers"][prov_id]
                    _write_cfg(mj_path, mj)
                    changed_any = True
            except Exception:
                pass

        if changed_any:
            updated.append(gw_dir.name)
    if updated:
        print(f"[sync-remove-provider] Cleaned provider '{prov_id}' from gateways: {updated}")


@app.route("/api/llm-profiles/providers/<prov_id>", methods=["DELETE"])
def delete_provider(prov_id):
    profiles = _read_llm_profiles()
    providers = profiles.get("providers", [])
    profiles["providers"] = [p for p in providers if p["id"] != prov_id]
    _save_llm_profiles(profiles)
    _sync_remove_provider_from_gateways(prov_id)
    return jsonify({"success": True})


@app.route("/api/llm-profiles/providers/<prov_id>/key", methods=["PUT"])
def set_provider_key(prov_id):
    """Set API key for a provider and propagate to all gateways."""
    api_key = (request.json or {}).get("api_key", "")
    if not api_key:
        return jsonify({"error": "api_key required"}), 400

    profiles = _read_llm_profiles()
    prov = next((p for p in profiles.get("providers", []) if p["id"] == prov_id), None)
    if not prov:
        return jsonify({"error": f"Provider '{prov_id}' not found"}), 404

    prov["api_key"] = api_key
    _save_llm_profiles(profiles)

    # Propagate to all gateway auth-profiles.json
    updated = []
    for gw_dir in GATEWAYS_DIR.iterdir():
        auth_path = gw_dir / "state" / "agents" / "main" / "agent" / "auth-profiles.json"
        if not auth_path.exists():
            continue
        try:
            auth_data = _read_cfg(auth_path)
            auth_profiles = auth_data.setdefault("profiles", {})
            auth_key = f"{prov_id}:default"
            if auth_key not in auth_profiles:
                auth_profiles[auth_key] = {
                    "type": prov.get("auth_mode", "api_key"),
                    "provider": prov_id,
                }
            auth_profiles[auth_key]["key"] = api_key
            _write_cfg(auth_path, auth_data)
            updated.append(gw_dir.name)
        except Exception:
            continue

    return jsonify({"success": True, "updated_gateways": updated})


@app.route("/api/gateways/<gw_id>/model", methods=["GET"])
def get_gateway_model(gw_id):
    """Get current model config from a gateway's openclaw.json."""
    gw = manager.get(gw_id)
    if not gw:
        return jsonify({"error": "Not found"}), 404
    cfg_path = HUB_DIR / gw.config_file
    cfg = _read_cfg(cfg_path)
    agents = cfg.get("agents", {}).get("defaults", {})
    model_cfg = agents.get("model", {})
    models_map = agents.get("models", {})
    return jsonify({
        "primary": model_cfg.get("primary", ""),
        "fallbacks": model_cfg.get("fallbacks", []),
        "models": models_map,
    })


@app.route("/api/gateways/<gw_id>/model", methods=["PUT"])
def set_gateway_model(gw_id):
    """Set primary model for a gateway. Updates openclaw.json + auth-profiles.json."""
    gw = manager.get(gw_id)
    if not gw:
        return jsonify({"error": "Not found"}), 404

    data = request.json or {}
    primary = data.get("primary", "").strip()
    if not primary or "/" not in primary:
        return jsonify({"error": "primary must be 'provider/model_id'"}), 400

    prov_id = primary.split("/")[0]

    # Load LLM profiles to get provider config
    llm_data = _read_llm_profiles()
    prov = next((p for p in llm_data.get("providers", []) if p["id"] == prov_id), None)
    if not prov:
        return jsonify({"error": f"Provider '{prov_id}' not found in LLM profiles"}), 404

    model_id = primary.split("/", 1)[1]
    model_def = next((m for m in prov.get("models", []) if m["id"] == model_id), None)
    if not model_def:
        return jsonify({"error": f"Model '{model_id}' not found in provider '{prov_id}'"}), 404

    # Validate against openclaw's supported model list
    if not _is_model_openclaw_compatible(prov_id, model_id):
        return jsonify({
            "error": f"模型 '{primary}' 不被当前 openclaw 版本支持。请选择其他模型或升级 openclaw。",
            "unsupported_model": True,
        }), 400

    # Read current openclaw.json
    cfg_path = HUB_DIR / gw.config_file
    cfg = _read_cfg(cfg_path)
    if not cfg:
        return jsonify({"error": "Cannot read openclaw.json"}), 500

    # Update primary model
    agents = cfg.setdefault("agents", {}).setdefault("defaults", {})
    model_section = agents.setdefault("model", {})
    model_section["primary"] = primary

    # Ensure fallbacks don't include the new primary
    fallbacks = model_section.get("fallbacks", [])
    fallbacks = [f for f in fallbacks if f != primary]
    model_section["fallbacks"] = fallbacks

    # Ensure model alias entry
    models_map = agents.setdefault("models", {})
    if primary not in models_map:
        models_map[primary] = {"alias": model_def.get("label", model_id)}

    # Ensure auth profile
    auth_profiles = cfg.setdefault("auth", {}).setdefault("profiles", {})
    auth_key = f"{prov_id}:default"
    if auth_key not in auth_profiles:
        auth_profiles[auth_key] = {
            "provider": prov_id,
            "mode": prov.get("auth_mode", "api_key"),
        }

    # Ensure custom provider in models.providers if it has base_url
    if prov.get("base_url"):
        providers_section = cfg.setdefault("models", {}).setdefault("providers", {})
        if prov_id not in providers_section:
            prov_entry = {
                "baseUrl": prov["base_url"],
            }
            if prov.get("api_format"):
                prov_entry["api"] = prov["api_format"]
            # Add model definitions
            prov_entry["models"] = []
            for m in prov.get("models", []):
                m_entry = {"id": m["id"], "name": m.get("label", m["id"])}
                for extra in ("reasoning", "input", "cost", "contextWindow", "maxTokens"):
                    if extra in m:
                        m_entry[extra] = m[extra]
                prov_entry["models"].append(m_entry)
            providers_section[prov_id] = prov_entry

    _write_cfg(cfg_path, cfg)

    # Update auth-profiles.json with API key
    api_key = prov.get("api_key", "")
    if api_key:
        auth_path = HUB_DIR / gw.state_dir / "agents" / "main" / "agent" / "auth-profiles.json"
        if auth_path.exists():
            try:
                auth_data = _read_cfg(auth_path)
                auth_profs = auth_data.setdefault("profiles", {})
                if auth_key not in auth_profs:
                    auth_profs[auth_key] = {
                        "type": prov.get("auth_mode", "api_key"),
                        "provider": prov_id,
                    }
                auth_profs[auth_key]["key"] = api_key
                _write_cfg(auth_path, auth_data)
            except Exception:
                pass

    return jsonify({"success": True, "primary": primary})


# ── Materials API (素材库) ─────────────────────────────────────

MATERIALS_DIR = HUB_DIR / "materials"
MATERIALS_INDEX = HUB_DIR / "materials_index.json"
MATERIALS_DIR.mkdir(exist_ok=True)


def _read_materials_index() -> list:
    if MATERIALS_INDEX.exists():
        try:
            return json.loads(MATERIALS_INDEX.read_text("utf-8"))
        except Exception:
            pass
    return []


def _save_materials_index(items: list):
    MATERIALS_INDEX.write_text(json.dumps(items, indent=2, ensure_ascii=False), "utf-8")


@app.route("/api/materials", methods=["GET"])
def list_materials():
    items = _read_materials_index()
    return jsonify({"materials": items})


@app.route("/api/materials", methods=["POST"])
def upload_material():
    """Upload a file to the materials library.
    Accepts multipart/form-data with 'file' field, or JSON with 'content' and 'filename'.
    """
    if request.content_type and "multipart" in request.content_type:
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "No file provided"}), 400
        original_name = f.filename
        content = f.read().decode("utf-8", errors="replace")
    else:
        data = request.json or {}
        content = data.get("content", "")
        original_name = data.get("filename", "untitled.md")
        if not content:
            return jsonify({"error": "No content provided"}), 400

    mat_id = uuid.uuid4().hex[:12]
    ext = Path(original_name).suffix or ".md"
    stored_name = f"{mat_id}{ext}"
    (MATERIALS_DIR / stored_name).write_text(content, "utf-8")

    # Determine material type: "knowledge" or "target"
    if request.content_type and "multipart" in request.content_type:
        mat_type = request.form.get("type", "knowledge")
    else:
        mat_type = data.get("type", "knowledge")
    if mat_type not in ("knowledge", "target"):
        mat_type = "knowledge"

    item = {
        "id": mat_id,
        "type": mat_type,
        "filename": original_name,
        "stored": stored_name,
        "size": len(content),
        "lines": content.count("\n") + 1,
        "uploaded_at": _time_mod.strftime("%Y-%m-%d %H:%M:%S"),
        "tags": [],
    }

    items = _read_materials_index()
    items.insert(0, item)
    _save_materials_index(items)
    return jsonify({"ok": True, "material": item})


@app.route("/api/materials/<mat_id>", methods=["GET"])
def get_material(mat_id):
    items = _read_materials_index()
    item = next((m for m in items if m["id"] == mat_id), None)
    if not item:
        return jsonify({"error": "Material not found"}), 404
    fpath = MATERIALS_DIR / item["stored"]
    content = fpath.read_text("utf-8") if fpath.exists() else ""
    return jsonify({**item, "content": content})


@app.route("/api/materials/<mat_id>", methods=["DELETE"])
def delete_material(mat_id):
    items = _read_materials_index()
    item = next((m for m in items if m["id"] == mat_id), None)
    if not item:
        return jsonify({"error": "Material not found"}), 404
    fpath = MATERIALS_DIR / item["stored"]
    if fpath.exists():
        fpath.unlink()
    items = [m for m in items if m["id"] != mat_id]
    _save_materials_index(items)
    return jsonify({"ok": True})


@app.route("/api/materials/<mat_id>/tags", methods=["PUT"])
def update_material_tags(mat_id):
    tags = (request.json or {}).get("tags", [])
    items = _read_materials_index()
    item = next((m for m in items if m["id"] == mat_id), None)
    if not item:
        return jsonify({"error": "Material not found"}), 404
    item["tags"] = tags
    _save_materials_index(items)
    return jsonify({"ok": True})


@app.route("/api/materials/<mat_id>/type", methods=["PUT"])
def update_material_type(mat_id):
    mat_type = (request.json or {}).get("type", "")
    if mat_type not in ("knowledge", "target"):
        return jsonify({"error": "type must be 'knowledge' or 'target'"}), 400
    items = _read_materials_index()
    item = next((m for m in items if m["id"] == mat_id), None)
    if not item:
        return jsonify({"error": "Material not found"}), 404
    item["type"] = mat_type
    _save_materials_index(items)
    return jsonify({"ok": True})


# ── Persona Library (角色库) ───────────────────────────────────

PERSONAS_DIR = HUB_DIR / "personas"
PERSONAS_INDEX = HUB_DIR / "personas_index.json"
PERSONAS_DIR.mkdir(exist_ok=True)

PERSONA_FILES = ("SOUL.md", "IDENTITY.md", "AGENTS.md")


def _read_personas_index() -> list:
    if PERSONAS_INDEX.exists():
        try:
            return json.loads(PERSONAS_INDEX.read_text("utf-8"))
        except Exception:
            pass
    return []


def _save_personas_index(items: list):
    PERSONAS_INDEX.write_text(json.dumps(items, indent=2, ensure_ascii=False), "utf-8")


def _save_persona_to_library(character_name: str, emoji: str, model: str,
                              source_gw_id: str, files: dict) -> dict:
    """Save a set of persona files to the library. Returns the saved item metadata."""
    persona_id = uuid.uuid4().hex[:12]
    persona_dir = PERSONAS_DIR / persona_id
    persona_dir.mkdir(parents=True, exist_ok=True)

    for filename, content in files.items():
        (persona_dir / filename).write_text(content, "utf-8")

    item = {
        "id": persona_id,
        "name": character_name,
        "emoji": emoji,
        "model": model,
        "source_gateway": source_gw_id,
        "files": list(files.keys()),
        "saved_at": _time_mod.strftime("%Y-%m-%d %H:%M:%S"),
    }

    items = _read_personas_index()
    items.insert(0, item)
    _save_personas_index(items)
    print(f"[persona_lib] Saved '{character_name}' ({persona_id}) with {len(files)} files")
    return item


@app.route("/api/personas", methods=["GET"])
def list_personas():
    items = _read_personas_index()
    return jsonify({"personas": items})


@app.route("/api/personas", methods=["POST"])
def save_persona():
    """Save current persona files from a gateway to the library.
    Body: {gateway_id, name?, emoji?}
    """
    data = request.json or {}
    gw_id = data.get("gateway_id", "")
    gw = manager.get(gw_id)
    if not gw:
        return jsonify({"error": "Gateway not found"}), 404

    ws_dir = HUB_DIR / gw.workspace_dir
    files = {}
    for fn in PERSONA_FILES:
        fpath = ws_dir / fn
        if fpath.exists():
            files[fn] = fpath.read_text("utf-8")

    if not files:
        return jsonify({"error": "该 Gateway 没有人格文件（SOUL.md 等）"}), 400

    name = data.get("name", "").strip() or gw.name or gw_id
    emoji = data.get("emoji", "").strip() or getattr(gw, "emoji", "🪭") or "🪭"
    model = data.get("model", "unknown")

    item = _save_persona_to_library(name, emoji, model, gw_id, files)
    return jsonify({"ok": True, "persona": item})


@app.route("/api/personas/<persona_id>", methods=["GET"])
def get_persona(persona_id):
    items = _read_personas_index()
    item = next((p for p in items if p["id"] == persona_id), None)
    if not item:
        return jsonify({"error": "Persona not found"}), 404

    persona_dir = PERSONAS_DIR / persona_id
    files = {}
    for fn in item.get("files", PERSONA_FILES):
        fpath = persona_dir / fn
        if fpath.exists():
            files[fn] = fpath.read_text("utf-8")

    return jsonify({**item, "contents": files})


@app.route("/api/personas/<persona_id>", methods=["DELETE"])
def delete_persona(persona_id):
    items = _read_personas_index()
    item = next((p for p in items if p["id"] == persona_id), None)
    if not item:
        return jsonify({"error": "Persona not found"}), 404

    persona_dir = PERSONAS_DIR / persona_id
    if persona_dir.exists():
        import shutil
        shutil.rmtree(persona_dir)

    items = [p for p in items if p["id"] != persona_id]
    _save_personas_index(items)
    return jsonify({"ok": True})


@app.route("/api/personas/<persona_id>/apply", methods=["POST"])
def apply_persona(persona_id):
    """Apply a saved persona to a gateway.
    Body: {gateway_id}
    """
    data = request.json or {}
    gw_id = data.get("gateway_id", "")
    gw = manager.get(gw_id)
    if not gw:
        return jsonify({"error": "Gateway not found"}), 404

    items = _read_personas_index()
    item = next((p for p in items if p["id"] == persona_id), None)
    if not item:
        return jsonify({"error": "Persona not found"}), 404

    persona_dir = PERSONAS_DIR / persona_id
    ws_dir = HUB_DIR / gw.workspace_dir
    ws_dir.mkdir(parents=True, exist_ok=True)

    applied = []
    for fn in item.get("files", PERSONA_FILES):
        src = persona_dir / fn
        if src.exists():
            dst = ws_dir / fn
            # Backup existing
            if dst.exists():
                bak = ws_dir / f"{fn}.bak.prev"
                dst.replace(bak)
            dst.write_text(src.read_text("utf-8"), "utf-8")
            applied.append(fn)

    # Update gateway name to match persona
    if item.get("name"):
        _update_gateway_name(gw_id, item["name"])

    return jsonify({"ok": True, "applied_files": applied, "persona_name": item["name"]})


@app.route("/api/personas/generate", methods=["POST"])
def generate_persona_to_library():
    """Start async persona generation directly to library.
    Body: {character_name, model?}
    Returns: {task_id, status}
    Multiple tasks can run concurrently.
    """
    import time as _time
    data = request.json or {}
    character_name = data.get("character_name", "").strip()
    if not character_name:
        return jsonify({"error": "角色名不能为空"}), 400
    model = data.get("model", "gpt-5.4")

    task_id = uuid.uuid4().hex[:12]
    _persona_gen_tasks[task_id] = {
        "status": "generating",
        "character_name": character_name,
        "model": model,
        "message": f"正在用 {model} 生成 {character_name}...",
        "_ts": _time.time(),
    }

    def _bg_gen():
        import time as _t
        start = _t.time()
        from generate_persona import generate_persona_standalone
        try:
            print(f"[persona_gen] Library task {task_id}: {character_name} model={model}")
            files = generate_persona_standalone(character_name, model=model)
            elapsed = _t.time() - start
            if not files:
                _persona_gen_tasks[task_id] = {
                    "status": "error",
                    "character_name": character_name,
                    "model": model,
                    "error": f"LLM ({model}) 未返回可解析的内容",
                }
                return
            # Save to library
            try:
                item = _save_persona_to_library(character_name, "🪭", model, "library", files)
                _persona_gen_tasks[task_id] = {
                    "status": "done",
                    "character_name": character_name,
                    "model": model,
                    "message": f"已生成「{character_name}」（{elapsed:.0f}秒），已存入角色库",
                    "persona_id": item["id"],
                    "files": {k: len(v) for k, v in files.items()},
                }
            except Exception as e:
                _persona_gen_tasks[task_id] = {
                    "status": "error",
                    "character_name": character_name,
                    "model": model,
                    "error": f"生成成功但保存失败: {e}",
                }
            print(f"[persona_gen] Library task {task_id}: DONE in {elapsed:.1f}s")
        except BaseException as e:
            elapsed = _t.time() - start
            print(f"[persona_gen] Library task {task_id}: ERROR after {elapsed:.1f}s: {e}")
            _persona_gen_tasks[task_id] = {
                "status": "error",
                "character_name": character_name,
                "model": model,
                "error": f"[{model}] {str(e)[:300]}",
            }

    threading.Thread(target=_bg_gen, daemon=True, name=f"persona-lib-{task_id}").start()
    return jsonify({"task_id": task_id, "status": "generating", "character_name": character_name}), 202


@app.route("/api/personas/generate/status", methods=["GET"])
def persona_gen_tasks_status():
    """Return all generation tasks (for polling). Auto-clears stale tasks."""
    import time as _time
    result = []
    stale_ids = []
    for tid, info in _persona_gen_tasks.items():
        if info.get("status") == "generating":
            elapsed = _time.time() - info.get("_ts", 0)
            if elapsed > _PERSONA_GEN_TIMEOUT:
                info = {**info, "status": "error", "error": f"生成超时（{int(elapsed)}秒）"}
                _persona_gen_tasks[tid] = info
        # Keep done/error tasks for 10 minutes then clean up
        if info.get("status") in ("done", "error"):
            ts = info.get("_ts", 0)
            if ts and (_time.time() - ts > 600):
                stale_ids.append(tid)
                continue
        result.append({k: v for k, v in info.items() if not k.startswith("_")})
        result[-1]["task_id"] = tid
    for sid in stale_ids:
        _persona_gen_tasks.pop(sid, None)
    return jsonify({"tasks": result})


# ── Arena Templates (讨论模板) ────────────────────────────────

ARENA_TEMPLATES_FILE = HUB_DIR / "arena_templates.json"


def _read_arena_templates() -> list:
    if ARENA_TEMPLATES_FILE.exists():
        try:
            return json.loads(ARENA_TEMPLATES_FILE.read_text("utf-8"))
        except Exception:
            pass
    return []


def _save_arena_templates(items: list):
    ARENA_TEMPLATES_FILE.write_text(json.dumps(items, indent=2, ensure_ascii=False), "utf-8")


@app.route("/api/arena/templates", methods=["GET"])
def list_arena_templates():
    return jsonify({"templates": _read_arena_templates()})


@app.route("/api/arena/templates", methods=["POST"])
def save_arena_template():
    """Save current Arena configuration as a reusable template.
    Body: {name, slots: [{gateway_id, persona_id, focus}], topic?, rounds?, maxReplyLength?,
           intervalSeconds?, knowledge_ids?, target_ids?}
    """
    data = request.json or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "模板名称不能为空"}), 400

    slots = data.get("slots", [])
    if not slots:
        return jsonify({"error": "至少需要一个专家配置"}), 400

    tpl = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "slots": slots,
        "topic": data.get("topic", ""),
        "herald_prompt": data.get("herald_prompt", ""),
        "rounds": data.get("rounds", 3),
        "maxReplyLength": data.get("maxReplyLength", 500),
        "intervalSeconds": data.get("intervalSeconds", 10),
        "knowledge_ids": data.get("knowledge_ids", []),
        "target_ids": data.get("target_ids", []),
        "saved_at": _time_mod.strftime("%Y-%m-%d %H:%M:%S"),
    }

    items = _read_arena_templates()
    items.insert(0, tpl)
    _save_arena_templates(items)
    return jsonify({"ok": True, "template": tpl})


@app.route("/api/arena/templates/<tpl_id>", methods=["DELETE"])
def delete_arena_template(tpl_id):
    items = _read_arena_templates()
    if not any(t["id"] == tpl_id for t in items):
        return jsonify({"error": "Template not found"}), 404
    items = [t for t in items if t["id"] != tpl_id]
    _save_arena_templates(items)
    return jsonify({"ok": True})


@app.route("/api/arena/templates/<tpl_id>/load", methods=["POST"])
def load_arena_template(tpl_id):
    """Load a template and optionally apply persona cards to gateways.
    Returns the template config for the frontend to populate the form.
    Also applies personas from slots where persona_id is set.
    """
    items = _read_arena_templates()
    tpl = next((t for t in items if t["id"] == tpl_id), None)
    if not tpl:
        return jsonify({"error": "Template not found"}), 404

    # Auto-apply personas from slots
    applied = []
    for slot in tpl.get("slots", []):
        gw_id = slot.get("gateway_id", "")
        persona_id = slot.get("persona_id", "")
        if not gw_id or not persona_id:
            continue
        gw = manager.get(gw_id)
        if not gw:
            continue
        # Find persona in library
        personas = _read_personas_index()
        persona = next((p for p in personas if p["id"] == persona_id), None)
        if not persona:
            continue
        persona_dir = PERSONAS_DIR / persona_id
        ws_dir = HUB_DIR / gw.workspace_dir
        ws_dir.mkdir(parents=True, exist_ok=True)
        files_applied = []
        for fn in persona.get("files", PERSONA_FILES):
            src = persona_dir / fn
            if src.exists():
                dst = ws_dir / fn
                if dst.exists():
                    bak = ws_dir / f"{fn}.bak.prev"
                    dst.replace(bak)
                dst.write_text(src.read_text("utf-8"), "utf-8")
                files_applied.append(fn)
        if persona.get("name"):
            _update_gateway_name(gw_id, persona["name"])
        applied.append({"gateway_id": gw_id, "persona_name": persona.get("name", ""), "files": files_applied})

    return jsonify({"ok": True, "template": tpl, "applied_personas": applied})


# ── OpenClaw model compatibility ──────────────────────────────

_openclaw_models_cache: dict = {}  # {"models": set(), "ts": float}


def _get_openclaw_supported_models(force: bool = False) -> set:
    """Query openclaw's built-in model catalog via CLI.

    Returns a set of 'provider/model_id' strings.  Cached for 1 hour.
    """
    import subprocess as sp
    from gateway_manager import _resolve_node_openclaw

    cache_ttl = 3600
    now = _time_mod.time()
    if not force and _openclaw_models_cache.get("models") and (now - _openclaw_models_cache.get("ts", 0)) < cache_ttl:
        return _openclaw_models_cache["models"]

    node_exe, openclaw_js = _resolve_node_openclaw()
    if not node_exe or not openclaw_js:
        return _openclaw_models_cache.get("models", set())

    try:
        r = sp.run(
            f'"{node_exe}" "{openclaw_js}" models list --all --json',
            shell=True, cwd=str(HUB_DIR),
            timeout=30, capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"[openclaw-models] list failed: {(r.stderr or '')[:200]}")
            return _openclaw_models_cache.get("models", set())

        # Parse JSON output — expected: array of model objects or similar
        raw = (r.stdout or "").strip()
        models_set = set()

        def _clean(s: str) -> str:
            return s.strip().strip('"').strip("'").rstrip(",").strip()

        def _add(prov: str, mid: str):
            prov, mid = _clean(prov), _clean(mid)
            if prov and mid:
                models_set.add(f"{prov}/{mid}".lower())
            elif mid and "/" in mid:
                models_set.add(mid.lower())

        def _extract_list(prov: str, items):
            for m in items:
                if isinstance(m, str):
                    _add(prov, m)
                elif isinstance(m, dict):
                    _add(prov, m.get("id") or m.get("model") or m.get("name") or "")

        # Try to locate first JSON object/array (skip non-JSON preamble)
        json_data = None
        for ch in ("{", "["):
            idx = raw.find(ch)
            if idx >= 0:
                try:
                    json_data = json.loads(raw[idx:])
                    break
                except json.JSONDecodeError:
                    continue

        if json_data is not None:
            data = json_data
            if isinstance(data, list):
                for entry in data:
                    if isinstance(entry, str):
                        clean = _clean(entry)
                        if "/" in clean:
                            models_set.add(clean.lower())
                        elif clean:
                            models_set.add(f"unknown/{clean}".lower())
                    elif isinstance(entry, dict):
                        mid = entry.get("id") or entry.get("model") or entry.get("name") or ""
                        prov = entry.get("provider") or entry.get("KEY") or entry.get("key") or ""
                        mlist = entry.get("models") or entry.get("items") or []
                        if mlist and prov:
                            _extract_list(_clean(prov), mlist)
                        elif mid and prov:
                            _add(prov, mid)
                        elif mid:
                            clean = _clean(mid)
                            if "/" in clean:
                                models_set.add(clean.lower())
            elif isinstance(data, dict):
                for prov_id, prov_data in data.items():
                    prov_id = _clean(prov_id)
                    if isinstance(prov_data, list):
                        # {"PROVIDER": ["model1", "model2", ...]}
                        _extract_list(prov_id, prov_data)
                    elif isinstance(prov_data, dict):
                        mlist = prov_data.get("models") or prov_data.get("items") or []
                        if mlist:
                            _extract_list(prov_id, mlist)
                        else:
                            mid = prov_data.get("id") or prov_data.get("model") or ""
                            if mid:
                                _add(prov_id, mid)
                    elif isinstance(prov_data, str):
                        if "/" in prov_id:
                            models_set.add(prov_id.lower())
        else:
            # Fallback: parse plain text (one model per line)
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                clean = _clean(line)
                if "/" in clean and not clean.startswith("{") and not clean.startswith("["):
                    models_set.add(clean.lower())

        if models_set:
            _openclaw_models_cache["models"] = models_set
            _openclaw_models_cache["ts"] = now
            print(f"[openclaw-models] Cached {len(models_set)} supported models")
        return models_set
    except Exception as e:
        print(f"[openclaw-models] Exception: {e}")
        return _openclaw_models_cache.get("models", set())


def _get_custom_provider_models() -> set:
    """Get models defined in template openclaw.json custom providers.

    These are always valid because openclaw loads them from config.
    """
    tpl_path = GATEWAYS_DIR / "_template" / "openclaw.json"
    if not tpl_path.exists():
        return set()
    try:
        cfg = _read_cfg(tpl_path)
        models_set = set()
        # Custom providers in models.providers
        for prov_id, prov_data in cfg.get("models", {}).get("providers", {}).items():
            if isinstance(prov_data, dict):
                for m in prov_data.get("models", []):
                    mid = m.get("id", "") if isinstance(m, dict) else str(m)
                    if mid:
                        models_set.add(f"{prov_id}/{mid}".lower())
        # Allowlist in agents.defaults.models
        for model_ref in cfg.get("agents", {}).get("defaults", {}).get("models", {}).keys():
            models_set.add(model_ref.lower())
        return models_set
    except Exception:
        return set()


def _is_model_openclaw_compatible(provider_id: str, model_id: str) -> bool:
    """Check if a model is supported by the current openclaw installation.

    A model is compatible if it's in openclaw's built-in catalog OR
    defined as a custom provider in the template config.
    """
    full_ref = f"{provider_id}/{model_id}".lower()

    # Check custom providers first (always valid)
    custom = _get_custom_provider_models()
    if full_ref in custom:
        return True

    # Check openclaw built-in catalog
    builtin = _get_openclaw_supported_models()
    if not builtin:
        # If we can't query openclaw, don't block — return True
        return True
    return full_ref in builtin


@app.route("/api/openclaw-models", methods=["GET"])
def list_openclaw_models():
    """Return the set of models supported by the current openclaw installation."""
    force = request.args.get("refresh") == "1"
    builtin = _get_openclaw_supported_models(force=force)
    custom = _get_custom_provider_models()
    all_models = sorted(builtin | custom)
    return jsonify({"models": all_models, "count": len(all_models)})


# ── Model Update Detection ────────────────────────────────────

def _probe_openai_models(api_key: str, proxy: str = "") -> list:
    """Query OpenAI /v1/models to discover latest GPT models."""
    req = urllib.request.Request(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy}) if proxy else urllib.request.ProxyHandler()
        )
        with opener.open(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        models = [m["id"] for m in data.get("data", [])]
        # Filter for latest GPT frontier models
        gpt5_models = sorted([m for m in models if m.startswith("gpt-5") and "chat" not in m and "codex" not in m and "audio" not in m and "realtime" not in m and "transcribe" not in m and "tts" not in m and "image" not in m and "oss" not in m and "nano" not in m and "mini" not in m], reverse=True)
        # Group by base version (e.g., gpt-5.4, gpt-5.4-pro)
        top_models = []
        seen_bases = set()
        for m in gpt5_models:
            # Skip dated snapshots like gpt-5.4-2026-03-05
            parts = m.split("-")
            if len(parts) >= 4 and parts[-1].isdigit() and len(parts[-1]) >= 2:
                continue
            base = m
            if base not in seen_bases:
                seen_bases.add(base)
                label = base.upper().replace("GPT-", "GPT-").replace("-PRO", " Pro")
                top_models.append({"id": base, "label": label})
        return top_models[:6]
    except Exception as e:
        print(f"[model-probe] OpenAI probe failed: {e}")
        return []


def _probe_google_models(api_key: str) -> list:
    """Query Google Gemini models API."""
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        models = data.get("models", [])
        # Filter for latest Gemini models (name format: models/gemini-X.Y-...)
        gemini_ids = []
        for m in models:
            name = m.get("name", "").replace("models/", "")
            if name.startswith("gemini-") and "exp" not in name and "embedding" not in name:
                gemini_ids.append({
                    "id": name,
                    "label": m.get("displayName", name),
                    "description": m.get("description", "")[:80],
                })
        # Sort by version descending
        gemini_ids.sort(key=lambda x: x["id"], reverse=True)
        return gemini_ids[:6]
    except Exception as e:
        print(f"[model-probe] Google probe failed: {e}")
        return []


# Known latest models for providers without discovery APIs
_KNOWN_LATEST = {
    "anthropic": [
        {"id": "claude-opus-4-6", "label": "Claude Opus 4.6"},
        {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6"},
    ],
    "kimi-coding": [
        {"id": "k2p5", "label": "Kimi K2.5"},
    ],
    "minimax": [
        {"id": "MiniMax-M2.5", "label": "MiniMax M2.5"},
    ],
}


@app.route("/api/llm-profiles/check-updates", methods=["POST"])
def check_model_updates():
    """Probe each provider for latest models, compare with current config."""
    profiles = _read_llm_profiles()
    providers = profiles.get("providers", [])
    proxy = "http://127.0.0.1:10020"

    results = []
    for prov in providers:
        pid = prov["id"]
        key = prov.get("api_key", "")
        current_models = {m["id"]: m for m in prov.get("models", [])}

        discovered = []
        source = "known"

        if pid == "openai" and key:
            discovered = _probe_openai_models(key, proxy)
            source = "api" if discovered else "failed"
        elif pid == "google" and key:
            discovered = _probe_google_models(key)
            source = "api" if discovered else "failed"

        # Fallback to known latest
        if not discovered and pid in _KNOWN_LATEST:
            discovered = _KNOWN_LATEST[pid]
            source = "known"

        # Compute diff: new models not in current config
        new_models = [m for m in discovered if m["id"] not in current_models]
        removed = [{"id": mid, "label": m.get("label", mid)} for mid, m in current_models.items() if mid not in {d["id"] for d in discovered}] if discovered else []

        # Annotate each model with openclaw compatibility
        for m in discovered + new_models + list(current_models.values()):
            mid = m.get("id", "")
            m["openclaw_supported"] = _is_model_openclaw_compatible(pid, mid)

        results.append({
            "provider_id": pid,
            "provider_label": prov.get("label", pid),
            "source": source,
            "current": list(current_models.values()),
            "discovered": discovered,
            "new": new_models,
            "removable": removed,
        })

    return jsonify({"updates": results})


@app.route("/api/llm-profiles/apply-updates", methods=["POST"])
def apply_model_updates():
    """Apply selected model updates.
    Body: {changes: [{provider_id, models: [{id, label}]}]}
    """
    data = request.json or {}
    changes = data.get("changes", [])
    if not changes:
        return jsonify({"error": "No changes provided"}), 400

    profiles = _read_llm_profiles()
    providers = {p["id"]: p for p in profiles.get("providers", [])}

    applied = []
    rejected = []
    all_removed_refs = set()
    for change in changes:
        pid = change.get("provider_id")
        new_models = change.get("models", [])
        if pid in providers and new_models:
            # Filter out models unsupported by openclaw
            supported = []
            for m in new_models:
                mid = m.get("id", "")
                if _is_model_openclaw_compatible(pid, mid):
                    supported.append(m)
                else:
                    rejected.append(f"{pid}/{mid}")
            if supported:
                # Detect removed models (in old list but not in new)
                old_ids = {m["id"] for m in providers[pid].get("models", [])}
                new_ids = {m["id"] for m in supported}
                for mid in old_ids - new_ids:
                    all_removed_refs.add(f"{pid}/{mid}")
                providers[pid]["models"] = supported
                applied.append(pid)

    profiles["providers"] = list(providers.values())
    _save_llm_profiles(profiles)

    # Sync removed models to all gateway configs
    if all_removed_refs:
        _sync_remove_models_from_gateways(all_removed_refs)

    resp = {"ok": True, "applied": applied}
    if all_removed_refs:
        resp["removed_from_gateways"] = sorted(all_removed_refs)
    if rejected:
        resp["rejected"] = rejected
        resp["warning"] = f"以下模型不被当前 openclaw 支持，已跳过: {', '.join(rejected)}"
    return jsonify(resp)


# ── Exec openclaw CLI ─────────────────────────────────────────
@app.route("/api/gateways/<gw_id>/exec", methods=["POST"])
def exec_command(gw_id):
    gw = manager.get(gw_id)
    if not gw:
        return jsonify({"error": "Not found"}), 404
    data = request.json or {}
    cmd_str = data.get("command", "").strip()
    if not cmd_str:
        return jsonify({"error": "command required"}), 400

    import subprocess as sp
    cfg_path = HUB_DIR / gw.config_file
    state_dir = HUB_DIR / gw.state_dir

    env = os.environ.copy()
    env["OPENCLAW_CONFIG_PATH"] = str(cfg_path)
    env["OPENCLAW_STATE_DIR"] = str(state_dir)
    env["HTTP_PROXY"] = "http://127.0.0.1:10020"
    env["HTTPS_PROXY"] = "http://127.0.0.1:10020"
    env["ALL_PROXY"] = "socks5://127.0.0.1:10020"

    # Prefix with "openclaw" if user didn't
    if not cmd_str.startswith("openclaw"):
        cmd_str = f"openclaw {cmd_str}"

    try:
        result = sp.run(
            cmd_str, shell=True, capture_output=True, text=True,
            timeout=30, env=env, cwd=str(HUB_DIR),
        )
        return jsonify({
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exitcode": result.returncode,
        })
    except sp.TimeoutExpired:
        return jsonify({"error": "Command timed out (30s)"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Start all / stop all ───────────────────────────────────────
@app.route("/api/start-all", methods=["POST"])
def start_all():
    results = []
    for gw in manager.list_all():
        ok, msg = gw.start()
        results.append({"id": gw.id, "ok": ok, "message": msg})
    return jsonify(results)


@app.route("/api/stop-all", methods=["POST"])
def stop_all():
    results = []
    for gw in manager.list_all():
        ok, msg = gw.stop()
        results.append({"id": gw.id, "ok": ok, "message": msg})
    return jsonify(results)


# ── Palace config ─────────────────────────────────────────────
@app.route("/api/palace", methods=["GET"])
def get_palace():
    return jsonify(_load_palace())


@app.route("/api/palace", methods=["PUT"])
def set_palace():
    data = request.json or {}
    palace_path = HUB_DIR / "palace.json"
    try:
        palace_path.write_text(
            json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8"
        )
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/palace/sync", methods=["POST"])
def trigger_sync():
    """Manually trigger one sync cycle."""
    try:
        sync_once()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


HERALD_GW_ID = "herald"

# Global flag to stop an ongoing multi-round discussion
_arena_stop = threading.Event()
_arena_running = threading.Event()  # set while a discussion is in progress
_arena_state: dict = {}  # {topic, expert_ids, rounds, current_round, message_count, started_at, summary}

# ── Arena Discussion API ──────────────────────────────────────

def _extract_last_assistant_reply(gw) -> str:
    """Read the gateway's session JSONL and return the last assistant text reply."""
    import re as _re
    state_dir = HUB_DIR / gw.state_dir
    sessions_dir = state_dir / "agents" / "main" / "sessions"
    sessions_json = sessions_dir / "sessions.json"
    if not sessions_json.exists():
        print(f"[arena] {gw.id}: no sessions.json")
        return ""
    try:
        raw = sessions_json.read_text(encoding="utf-8")
        if raw and ord(raw[0]) == 0xFEFF:
            raw = raw[1:]
        sessions = json.loads(raw)
    except Exception as e:
        print(f"[arena] {gw.id}: sessions.json parse error: {e}")
        return ""

    # Find session file: try sessionFile first, then construct from sessionId
    session_file = None
    for key, info in sessions.items():
        if not isinstance(info, dict):
            continue
        sf = info.get("sessionFile", "")
        sid = info.get("sessionId", "")
        if sf:
            p = Path(sf) if Path(sf).is_absolute() else sessions_dir / sf
            if p.exists():
                session_file = p
                break
        elif sid:
            p = sessions_dir / f"{sid}.jsonl"
            if p.exists():
                session_file = p
                break
    if not session_file:
        # Fallback: find most recently modified .jsonl in sessions dir
        jsonls = sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if jsonls:
            session_file = jsonls[0]
    if not session_file:
        print(f"[arena] {gw.id}: no session file found")
        return ""

    print(f"[arena] {gw.id}: reading {session_file.name}")

    # Read all lines, search backwards for last assistant message WITH text content
    last_assistant = ""
    try:
        with open(session_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "message":
                continue
            msg = entry.get("message", {})
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", [])
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        parts.append(part.get("text", ""))
                text = "\n".join(parts).strip()
            # Skip messages that have no text (tool calls only, thinking only)
            if text and text not in ("NO_REPLY", "HEARTBEAT_OK"):
                last_assistant = text
                break
    except Exception as e:
        print(f"[arena] {gw.id}: session read error: {e}")

    if not last_assistant:
        print(f"[arena] {gw.id}: no text reply found in session")
        return ""

    # Clean up tags
    text = last_assistant.strip()
    text = _re.sub(r'</?final>', '', text).strip()
    text = _re.sub(r'\[DM:\w+\].*?(?=\[(?:DM|WHISPER):|\Z)', '', text, flags=_re.DOTALL).strip()
    text = _re.sub(r'\[WHISPER:\w+\].*?(?=\[(?:DM|WHISPER):|\Z)', '', text, flags=_re.DOTALL).strip()

    if not text:
        return ""
    print(f"[arena] {gw.id}: extracted reply ({len(text)} chars)")
    return text


def _post_reply_to_group(gw_id: str, text: str, sender_name: str = ""):
    """Send a bot's reply to the Telegram group, prefixed with sender name."""
    palace = _load_palace()
    group_id = palace.get("telegram", {}).get("groupId", "")
    if not group_id:
        return

    # Prefix with sender name so readers know who's speaking
    if sender_name:
        text = f"【{sender_name}】\n{text}"

    # Find bot token
    bot_token = ""
    if gw_id == HERALD_GW_ID:
        bot_token = palace.get("herald", {}).get("botToken", "")
    else:
        for p in palace.get("participants", []):
            if p["id"] == gw_id:
                bot_token = p.get("botToken", "")
                break
    if not bot_token:
        bot_token = _read_gateway_bot_token(gw_id)
    if not bot_token:
        print(f"[arena] Cannot post to group: no token for {gw_id}")
        return

    # Telegram message limit is 4096 chars
    if len(text) > 4000:
        text = text[:4000] + "..."

    result = _tg_api_call(bot_token, "sendMessage", {
        "chat_id": group_id,
        "text": text,
    })
    if result.get("ok"):
        print(f"[arena] {gw_id} posted to group ({len(text)} chars)")
    else:
        print(f"[arena] {gw_id} failed to post: {result.get('description', '?')}")


def _switch_to_fallback_model(gw) -> str:
    """Switch gateway primary model to the first configured fallback.

    Returns the new model name on success, else empty string.
    """
    cfg_path = HUB_DIR / gw.config_file
    if not cfg_path.exists():
        return ""
    try:
        raw = cfg_path.read_text(encoding="utf-8")
        if raw and ord(raw[0]) == 0xFEFF:
            raw = raw[1:]
        cfg = json.loads(raw)
    except Exception:
        return ""

    model_cfg = cfg.setdefault("agents", {}).setdefault("defaults", {}).setdefault("model", {})
    primary = str(model_cfg.get("primary", "") or "").strip()
    fallbacks = model_cfg.get("fallbacks") or []
    if not isinstance(fallbacks, list):
        fallbacks = []

    next_model = ""
    for fb in fallbacks:
        fb = str(fb or "").strip()
        if fb and fb != primary:
            next_model = fb
            break
    if not next_model:
        return ""

    model_cfg["primary"] = next_model
    try:
        cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[heartbeat] {gw.id} model fallback: {primary} -> {next_model}")
        return next_model
    except Exception as e:
        print(f"[heartbeat] {gw.id} failed to write model fallback: {e}")
        return ""


def _trigger_gateway_heartbeat(gw, message: str, timeout: int = 180):
    """Trigger a single gateway's heartbeat via openclaw CLI. Blocking.

    Uses node.exe + dist/index.js directly (same as gw.start()) to avoid
    npx cache lock contention when multiple instances run in parallel.
    """
    import subprocess as sp
    from gateway_manager import _resolve_node_openclaw

    cfg_path = HUB_DIR / gw.config_file
    state_dir = HUB_DIR / gw.state_dir
    env = os.environ.copy()
    env["OPENCLAW_CONFIG_PATH"] = str(cfg_path)
    env["OPENCLAW_STATE_DIR"] = str(state_dir)
    env["HTTP_PROXY"] = "http://127.0.0.1:10020"
    env["HTTPS_PROXY"] = "http://127.0.0.1:10020"
    env["ALL_PROXY"] = "socks5://127.0.0.1:10020"
    # Sanitize message for Windows shell: newlines and quotes break cmd.exe
    msg_safe = message.replace('\r', ' ').replace('\n', ' ').replace('"', '\\"')

    node_exe, openclaw_js = _resolve_node_openclaw()
    if node_exe and openclaw_js:
        cmd = f'"{node_exe}" "{openclaw_js}" agent --local --agent main --message "{msg_safe}"'
    else:
        cmd = f'npx openclaw agent --local --agent main --message "{msg_safe}"'
    try:
        r = sp.run(
            cmd,
            shell=True, env=env, cwd=str(HUB_DIR),
            timeout=timeout, capture_output=True, text=True,
        )
        if r.returncode != 0:
            combined = (r.stderr or "") + "\n" + (r.stdout or "")
            err = combined[:300]
            print(f"[heartbeat] {gw.id} FAILED (rc={r.returncode}): {err}")

            # Some gateways may keep stale/unsupported model ids.
            # Auto-switch to first configured fallback and retry once.
            if "Unknown model" in combined:
                switched = _switch_to_fallback_model(gw)
                if switched:
                    r2 = sp.run(
                        cmd,
                        shell=True, env=env, cwd=str(HUB_DIR),
                        timeout=timeout, capture_output=True, text=True,
                    )
                    if r2.returncode == 0:
                        print(f"[heartbeat] {gw.id} retry success with fallback model: {switched}")
                        return True, "ok"
                    combined2 = (r2.stderr or "") + "\n" + (r2.stdout or "")
                    print(f"[heartbeat] {gw.id} retry FAILED (rc={r2.returncode}): {combined2[:300]}")
                    return False, (r2.stderr or r2.stdout or "")[:200]

        return r.returncode == 0, (r.stderr or r.stdout or "")[:200] if r.returncode != 0 else "ok"
    except Exception as e:
        print(f"[heartbeat] {gw.id} EXCEPTION: {e}")
        return False, str(e)


def _clear_gateway_sessions(gw):
    """Reset session pointer so next heartbeat starts fresh, but keep old JSONL
    files so ThinkingPanel can still display past thinking blocks."""
    sessions_dir = HUB_DIR / gw.state_dir / "agents" / "main" / "sessions"
    sessions_json = sessions_dir / "sessions.json"
    if sessions_json.exists():
        try:
            sessions_json.unlink()
            print(f"[arena] Reset session pointer for {gw.id}")
        except Exception as e:
            print(f"[arena] Failed to reset sessions for {gw.id}: {e}")
    elif not sessions_dir.exists():
        sessions_dir.mkdir(parents=True, exist_ok=True)


def _write_arena_chat_log(topic, discussion_log, expert_ids, knowledge_text="", target_text="", focus_map=None):
    """Directly write GROUP_CHAT_LOG.md to all gateway workspaces from collected replies."""
    from datetime import datetime, timezone, timedelta

    lines = [
        "# 群组讨论记录",
        "",
        f"**当前议题**: {topic}",
        "",
    ]

    # Include discussion target (标的) — the subject to discuss
    if target_text:
        lines.extend([
            "## 📌 讨论标的",
            "",
            "以下是本次讨论的标的，请围绕此内容展开讨论：",
            "",
            target_text,
            "",
            "---",
            "",
        ])

    # Include reference knowledge (知识) — background info for reference
    if knowledge_text:
        lines.extend([
            "## 📚 参考知识",
            "",
            "以下是参考知识，请在回答时参考：",
            "",
            knowledge_text,
            "",
            "---",
            "",
        ])

    # Include focus assignments (主攻方向)
    if focus_map:
        focus_lines = []
        for eid in expert_ids:
            if eid in focus_map and focus_map[eid].strip():
                gw_obj = manager.get(eid)
                ename = gw_obj.name if gw_obj and hasattr(gw_obj, 'name') else eid
                focus_lines.append(f"- **{ename}**：{focus_map[eid].strip()}")
        if focus_lines:
            lines.extend([
                "## 🎯 各专家主攻方向",
                "",
                "每位专家请围绕自己的主攻方向坚持己见，不要被其他人带偏：",
                "",
            ] + focus_lines + [
                "",
                "---",
                "",
            ])

    lines.extend([
        "以下是群组中最近的对话。你可以看到所有参与者的发言。",
        "请直接输出你的回复内容，系统会自动发送到群。",
        "",
        "## 最近对话",
        "",
    ])

    if not discussion_log:
        lines.append("（暂无消息）")
    else:
        for msg in discussion_log:
            t = msg.get("time", "??:??")
            sender = msg.get("sender", "?")
            text = msg.get("text", "")
            # Truncate long messages for log
            if len(text) > 300:
                text = text[:300] + "..."
            text_oneline = text.replace("\n", " ").strip()
            lines.append(f"[{t}] {sender}: {text_oneline}")

    lines.append("")
    content = "\n".join(lines)

    # Write to all gateway workspaces (experts + herald)
    for ws_dir in GATEWAYS_DIR.iterdir():
        if not ws_dir.is_dir() or ws_dir.name.startswith("_"):
            continue
        log_path = ws_dir / "state" / "workspace" / "GROUP_CHAT_LOG.md"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(content, encoding="utf-8")
        except Exception:
            pass

    print(f"[arena] Wrote GROUP_CHAT_LOG.md ({len(discussion_log)} messages)")


def _run_arena_discussion(topic, expert_ids, rounds, max_reply=500, interval=10, knowledge_text="", target_text="", focus_map=None, herald_prompt=""):
    """Background coordinator: run multi-round discussion."""
    import time as _time
    from datetime import datetime, timezone, timedelta

    if focus_map is None:
        focus_map = {}

    _arena_stop.clear()
    _arena_running.set()

    # Accumulate all messages across rounds
    discussion_log = []
    auto_started_ids = []  # track which gateways we started so we can stop them later

    def _now_str():
        return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%H:%M")

    # Initialize tracking state
    _arena_state.update({
        "topic": topic,
        "expert_ids": list(expert_ids),
        "rounds": rounds,
        "current_round": 0,
        "message_count": 0,
        "started_at": _now_str(),
        "summary": None,
        "herald_prompt": herald_prompt,
    })

    try:
        # Step 0: Auto-start gateways that aren't running
        all_ids_to_start = list(expert_ids) + [HERALD_GW_ID]
        for gw_id in all_ids_to_start:
            gw = manager.get(gw_id)
            if gw and gw.status != "running":
                ok, msg = gw.start()
                if ok:
                    auto_started_ids.append(gw_id)
                    print(f"[arena] Auto-started gateway: {gw_id}")
                else:
                    print(f"[arena] Failed to start {gw_id}: {msg}")

        # Wait for auto-started gateways to be ready
        if auto_started_ids:
            print(f"[arena] Waiting for {len(auto_started_ids)} gateways to start...")
            _time.sleep(5)

        # Step 1: Herald announces topic
        herald_gw = manager.get(HERALD_GW_ID)
        if herald_gw:
            _clear_gateway_sessions(herald_gw)
            print(f"[arena] Herald announcing topic...")
            herald_prompt = f"新的讨论议题：{topic}"
            if target_text:
                herald_prompt += f"\n\n📌 本次讨论标的（专家需围绕此内容展开讨论）：\n{target_text[:2000]}"
            if knowledge_text:
                herald_prompt += f"\n\n📚 参考知识（供专家答题时参考）：\n{knowledge_text[:1000]}"
            if focus_map:
                focus_lines = []
                for eid in expert_ids:
                    if eid in focus_map and focus_map[eid].strip():
                        gw_obj = manager.get(eid)
                        ename = gw_obj.name if gw_obj and hasattr(gw_obj, 'name') else eid
                        focus_lines.append(f"- {ename}：{focus_map[eid].strip()}")
                if focus_lines:
                    herald_prompt += "\n\n🎯 各专家主攻方向分工：\n" + "\n".join(focus_lines)
            herald_prompt += "\n\n请直接输出你要发到群里的公告内容，系统会自动发送到 Telegram 群。不需要你知道群 ID，只需写出公告文字即可。"
            ok, msg = _trigger_gateway_heartbeat(
                herald_gw,
                herald_prompt,
                timeout=90,
            )
            if ok:
                reply = _extract_last_assistant_reply(herald_gw)
                if reply:
                    herald_name = herald_gw.name if hasattr(herald_gw, 'name') else "主持人"
                    _post_reply_to_group(HERALD_GW_ID, reply, sender_name=herald_name)
                    discussion_log.append({"time": _now_str(), "sender": herald_name, "text": reply})
                print(f"[arena] Herald announced topic: {topic[:50]}")
            else:
                print(f"[arena] Herald announce failed: {msg}")

        if _arena_stop.is_set():
            print("[arena] Stopped before round 1")
            return

        # Step 2: Multi-round expert discussion
        for rnd in range(1, rounds + 1):
            if _arena_stop.is_set():
                print(f"[arena] Stopped before round {rnd}")
                break

            _arena_state["current_round"] = rnd
            print(f"[arena] ── Round {rnd}/{rounds} ──")

            # Write latest discussion log to GROUP_CHAT_LOG.md for all gateways
            _write_arena_chat_log(topic, discussion_log, expert_ids, knowledge_text, target_text, focus_map)

            # Get fresh gateway list each round
            # Note: heartbeat uses 'openclaw agent --local' which doesn't need the
            # gateway HTTP server, so we don't filter by status == "running"
            all_gws = manager.list_all()
            running_experts = [gw for gw in all_gws if gw.id in expert_ids and gw.id != HERALD_GW_ID]
            print(f"[arena] R{rnd} experts: {[gw.id for gw in running_experts]} (status: {[(gw.id, gw.status) for gw in running_experts]})")

            # Clear sessions so each expert starts fresh and reads updated GROUP_CHAT_LOG.md
            for gw in running_experts:
                _clear_gateway_sessions(gw)

            length_hint = f"⚠️ 重要：回复控制在{max_reply}字以内，像真人群聊一样简短有力。只挑1-2个最值得回应的观点，不要面面俱到。"
            mat_hints = ""
            if target_text:
                mat_hints += "\n\n📌 讨论标的已附在 GROUP_CHAT_LOG.md 开头，请围绕标的内容展开讨论。"
            if knowledge_text:
                mat_hints += "\n\n📚 参考知识已附在 GROUP_CHAT_LOG.md 中，请在回答时参考。"

            def _build_expert_prompt(gw_id, rnd_num):
                focus = focus_map.get(gw_id, "").strip()
                focus_hint = f"\n\n🎯 你的主攻方向：{focus}。请始终围绕这个角度发表观点，坚持己见，不要被其他专家带偏。" if focus else ""
                if rnd_num == 1:
                    return f"Arena 新议题：{topic}。请阅读 GROUP_CHAT_LOG.md 中的群组讨论记录并参与讨论。直接输出你的发言内容，系统会自动发送到群。{mat_hints}{focus_hint}{length_hint}"
                else:
                    return f"讨论继续（第{rnd_num}轮）。请阅读 GROUP_CHAT_LOG.md 中最新的群组讨论记录，挑1-2个最值得回应的观点进行回应或反驳。直接输出你的发言内容，系统会自动发送到群。{focus_hint}{length_hint}"

            # Trigger all experts in parallel, collect replies
            results_lock = threading.Lock()
            round_replies = {}

            def _trigger_one(gw_obj, prompt_text, round_num):
                ok, msg = _trigger_gateway_heartbeat(gw_obj, prompt_text)
                reply = ""
                if ok:
                    reply = _extract_last_assistant_reply(gw_obj)
                    if reply:
                        gw_name = gw_obj.name if hasattr(gw_obj, 'name') else gw_obj.id
                        _post_reply_to_group(gw_obj.id, reply, sender_name=gw_name)
                with results_lock:
                    round_replies[gw_obj.id] = {"ok": ok, "reply": reply}
                if ok:
                    print(f"[arena] R{round_num} {gw_obj.id}: replied ({len(reply)} chars)" if reply else f"[arena] R{round_num} {gw_obj.id}: no reply")
                else:
                    print(f"[arena] R{round_num} {gw_obj.id}: failed: {msg}")

            threads = []
            for idx, gw in enumerate(running_experts):
                expert_prompt = _build_expert_prompt(gw.id, rnd)
                t = threading.Thread(target=_trigger_one, args=(gw, expert_prompt, rnd), daemon=True, name=f"arena-R{rnd}-{gw.id}")
                t.start()
                threads.append(t)
                # Stagger launches to avoid subprocess contention on Windows
                if idx < len(running_experts) - 1:
                    _time.sleep(2)

            # Wait for all experts to finish this round
            for t in threads:
                t.join(timeout=300)

            # Collect replies into discussion log
            for gw in running_experts:
                info = round_replies.get(gw.id, {})
                reply = info.get("reply", "")
                if reply:
                    gw_name = gw.name if hasattr(gw, 'name') else gw.id
                    discussion_log.append({"time": _now_str(), "sender": gw_name, "text": reply})

            replied_count = sum(1 for r in round_replies.values() if r.get("reply"))
            _arena_state["message_count"] = len(discussion_log)
            print(f"[arena] Round {rnd} done: {replied_count}/{len(running_experts)} replied")

            # Pause between rounds
            if rnd < rounds and not _arena_stop.is_set() and interval > 0:
                print(f"[arena] Waiting {interval}s before next round...")
                _arena_stop.wait(interval)  # interruptible sleep

        # Final write so GROUP_CHAT_LOG.md has the complete discussion
        _write_arena_chat_log(topic, discussion_log, expert_ids, knowledge_text, target_text, focus_map)
        print(f"[arena] Discussion finished ({rounds} rounds, {len(discussion_log)} total messages)")
    except Exception as e:
        import traceback
        print(f"[arena] Discussion error: {e}\n{traceback.format_exc()}")
    finally:
        _arena_running.clear()
        # Auto-stop gateways that we auto-started (skip herald — summary thread needs it)
        if auto_started_ids:
            stop_ids = [gid for gid in auto_started_ids if gid != HERALD_GW_ID]
            print(f"[arena] Auto-stopping {len(stop_ids)} expert gateways (herald kept for summary)...")
            for gw_id in stop_ids:
                gw = manager.get(gw_id)
                if gw and gw.status == "running":
                    try:
                        gw.stop()
                        print(f"[arena] Stopped {gw_id}")
                    except Exception:
                        pass


@app.route("/api/arena/start", methods=["POST"])
def arena_start():
    """Start a multi-round Arena discussion.

    Body: {topic, participant_ids?: [...], rounds?: 3}
    Announces topic via herald, then runs multiple rounds of expert heartbeats.
    """
    data = request.json or {}
    topic = data.get("topic", "").strip()
    if not topic:
        return jsonify({"error": "topic required"}), 400

    rounds = max(1, min(20, int(data.get("rounds", 3))))
    max_reply = max(100, min(5000, int(data.get("maxReplyLength", 500))))
    interval = max(0, min(120, int(data.get("intervalSeconds", 10))))

    if _arena_running.is_set():
        return jsonify({"error": "讨论正在进行中，请先结束当前讨论"}), 409

    participant_ids = data.get("participant_ids")
    knowledge_ids = data.get("knowledge_ids", [])
    target_ids = data.get("target_ids", [])
    focus_map = data.get("focus_map", {})
    herald_prompt = data.get("herald_prompt", "").strip()

    # Load materials content, separated by role
    knowledge_text = ""
    target_text = ""
    all_mat_ids = set(knowledge_ids) | set(target_ids)
    if all_mat_ids:
        mat_items = _read_materials_index()
        mat_map = {m["id"]: m for m in mat_items}
        k_parts, t_parts = [], []
        for mid in knowledge_ids:
            mat = mat_map.get(mid)
            if mat:
                fpath = MATERIALS_DIR / mat["stored"]
                if fpath.exists():
                    k_parts.append(f"### {mat['filename']}\n\n{fpath.read_text('utf-8')}")
        for mid in target_ids:
            mat = mat_map.get(mid)
            if mat:
                fpath = MATERIALS_DIR / mat["stored"]
                if fpath.exists():
                    t_parts.append(f"### {mat['filename']}\n\n{fpath.read_text('utf-8')}")
        if k_parts:
            knowledge_text = "\n\n---\n\n".join(k_parts)
        if t_parts:
            target_text = "\n\n---\n\n".join(t_parts)

    all_gws = manager.list_all()
    if participant_ids:
        experts = [gw for gw in all_gws if gw.id in participant_ids and gw.id != HERALD_GW_ID]
    else:
        experts = [gw for gw in all_gws if gw.id != HERALD_GW_ID]

    if not experts:
        return jsonify({"error": "No expert gateways available"}), 400

    expert_ids = [gw.id for gw in experts]

    # Launch the multi-round coordinator in background (will auto-start gateways)
    threading.Thread(
        target=_run_arena_discussion,
        args=(topic, expert_ids, rounds, max_reply, interval, knowledge_text, target_text, focus_map, herald_prompt),
        daemon=True, name="arena-coordinator",
    ).start()

    results = {
        "topic": topic,
        "rounds": rounds,
        "participants": [{"id": gw.id, "status": gw.status} for gw in experts],
        "herald_announced": bool(manager.get(HERALD_GW_ID)),
    }
    print(f"[arena] Discussion started: {topic[:80]} ({len(experts)} experts, {rounds} rounds)")
    return jsonify(results)


@app.route("/api/arena/nudge", methods=["POST"])
def arena_nudge():
    """Nudge all running expert gateways to continue the discussion.

    Body: {message?: "optional prompt", participant_ids?: ["id1","id2"]}
    Triggers another round of heartbeats so experts respond to new messages.
    """
    data = request.json or {}
    message = data.get("message", "请阅读 HEARTBEAT.md，查看最新讨论进展并回应。").strip()
    participant_ids = data.get("participant_ids")

    all_gws = manager.list_all()
    if participant_ids:
        experts = [gw for gw in all_gws if gw.id in participant_ids and gw.id != HERALD_GW_ID]
    else:
        experts = [gw for gw in all_gws if gw.id != HERALD_GW_ID]

    def _nudge_expert(gw_obj):
        _clear_gateway_sessions(gw_obj)
        ok, msg = _trigger_gateway_heartbeat(gw_obj, message + " 直接输出你的发言内容，系统会自动发送到群。")
        if ok:
            print(f"[arena] nudge {gw_obj.id}: triggered")
            reply = _extract_last_assistant_reply(gw_obj)
            if reply:
                gw_name = gw_obj.name if hasattr(gw_obj, 'name') else gw_obj.id
                _post_reply_to_group(gw_obj.id, reply, sender_name=gw_name)
        else:
            print(f"[arena] nudge {gw_obj.id}: failed: {msg}")

    results = []
    for gw in experts:
        if gw.status == "running":
            threading.Thread(
                target=_nudge_expert, args=(gw,),
                daemon=True, name=f"nudge-{gw.id}",
            ).start()
            results.append({"id": gw.id, "status": "nudged"})
        else:
            results.append({"id": gw.id, "status": "not_running"})

    return jsonify({"results": results, "message": message})


@app.route("/api/arena/end", methods=["POST"])
def arena_end():
    """End the current Arena discussion.
    Sets stop flag, optionally has herald summarize, clears logs.
    Body: {clear_logs?: bool}
    """
    data = request.json or {}
    clear_logs = data.get("clear_logs", False)

    # Signal the coordinator to stop
    was_running = _arena_running.is_set()
    _arena_stop.set()

    results = {"herald_announced": False, "logs_cleared": False, "was_running": was_running}

    # Herald auto-summary disabled — use the Secretary panel (📝 书记) instead.
    # Kept for reference but no longer triggered automatically.
    herald_gw = manager.get(HERALD_GW_ID)
    if False and herald_gw and herald_gw.status == "running" and not clear_logs:
        def _generate_summary():
            import time as _t
            # Wait for coordinator thread to finish writing final chat log
            for _ in range(60):
                if not _arena_running.is_set():
                    break
                _t.sleep(0.5)
            _t.sleep(1)  # extra buffer for final file write

            # Read the full discussion log
            chat_log_path = HUB_DIR / herald_gw.state_dir / "workspace" / "GROUP_CHAT_LOG.md"
            chat_content = ""
            if chat_log_path.exists():
                try:
                    chat_content = chat_log_path.read_text(encoding="utf-8")
                except Exception:
                    pass

            topic = _arena_state.get("topic", "未知议题")
            expert_count = len(_arena_state.get("expert_ids", []))
            total_rounds = _arena_state.get("rounds", 0)
            msg_count = _arena_state.get("message_count", 0)
            user_direction = _arena_state.get("herald_prompt", "")

            # Build summary direction: user-specified or default structure
            if user_direction:
                direction_section = f"""## ⚡ 用户指定的总结方向（最高优先级）

{user_direction}

请严格按照上述方向来组织总结。在此基础上，你也可以补充以下内容（但上面的方向是核心）：
- 各方立场和分歧点
- 最有价值的观点
- 一句话总结"""
            else:
                direction_section = """## 总结报告要求

请按以下结构输出总结（用中文）：

### 📋 议题回顾
简述本次讨论的核心议题。

### 🔥 争论焦点
列出讨论中出现的 2-3 个核心分歧点，每个分歧点说明各方的立场。

### 🧠 思路脉络
梳理讨论的演进过程：从最初的观点碰撞，到中间的深入交锋，到最后的立场变化。

### 🏆 评判
客观评价各方表现：谁的论证更有力？谁的视角更独特？谁更胜一筹？给出理由。

### 💡 关键洞见
提炼 2-3 条最有价值的观点或洞见，这些是本次讨论的核心收获。

### 📝 一句话总结
用一句话概括本次讨论的结论或核心分歧。"""

            summary_prompt = f"""本次 Arena 讨论已结束，请作为主持人输出一份**完整的总结报告**。

## 讨论基本信息
- 议题：{topic}
- 参与专家数：{expert_count}
- 讨论轮数：{total_rounds}
- 总发言数：{msg_count}

## 完整讨论记录
{chat_content if chat_content else "（讨论记录为空）"}

{direction_section}

请直接输出总结报告内容，系统会自动发送到群。"""

            _clear_gateway_sessions(herald_gw)
            try:
                ok, msg = _trigger_gateway_heartbeat(herald_gw, summary_prompt, timeout=180)
                if ok:
                    reply = _extract_last_assistant_reply(herald_gw)
                    if reply:
                        herald_name = herald_gw.name if hasattr(herald_gw, 'name') else "主持人"
                        _post_reply_to_group(HERALD_GW_ID, reply, sender_name=herald_name)
                        _arena_state["summary"] = reply
                        # Save summary to file
                        try:
                            summary_path = HUB_DIR / "backend" / "last_summary.md"
                            summary_path.write_text(f"# 讨论总结 — {topic}\n\n{reply}", encoding="utf-8")
                            print(f"[arena] Summary saved to last_summary.md ({len(reply)} chars)")
                        except Exception:
                            pass
                        print(f"[arena] Herald summary posted ({len(reply)} chars)")
                    else:
                        print("[arena] Herald summary: no reply extracted")
                else:
                    print(f"[arena] Herald summary failed: {msg}")
            finally:
                # Now safe to stop the herald
                try:
                    h = manager.get(HERALD_GW_ID)
                    if h and h.status == "running":
                        h.stop()
                        print(f"[arena] Herald stopped after summary")
                except Exception:
                    pass

        threading.Thread(target=_generate_summary, daemon=True, name="arena-summary").start()
        results["herald_announced"] = True

    # Clear GROUP_CHAT_LOG.md files if requested
    if clear_logs:
        _clear_group_chat_logs()
        results["logs_cleared"] = True

    print(f"[arena] Discussion ended (clear_logs={clear_logs})")
    return jsonify(results)


@app.route("/api/arena/status", methods=["GET"])
def arena_status():
    """Check if a discussion is currently running, with progress details."""
    running = _arena_running.is_set()
    result = {"running": running}
    if _arena_state:
        result["topic"] = _arena_state.get("topic", "")
        result["rounds"] = _arena_state.get("rounds", 0)
        result["current_round"] = _arena_state.get("current_round", 0)
        result["message_count"] = _arena_state.get("message_count", 0)
        result["expert_count"] = len(_arena_state.get("expert_ids", []))
        result["started_at"] = _arena_state.get("started_at", "")
        if _arena_state.get("summary"):
            result["summary"] = _arena_state["summary"]
    return jsonify(result)


# ── Arena Secretary (offline summary via direct LLM call) ─────

_secretary_status: dict = {}  # {status, result, error, _ts}


@app.route("/api/arena/secretary/log", methods=["GET"])
def secretary_get_log():
    """Return the current GROUP_CHAT_LOG.md content from the herald workspace."""
    herald_gw = manager.get(HERALD_GW_ID)
    log_content = ""
    if herald_gw:
        log_path = HUB_DIR / herald_gw.state_dir / "workspace" / "GROUP_CHAT_LOG.md"
        if log_path.exists():
            try:
                log_content = log_path.read_text(encoding="utf-8")
            except Exception:
                pass
    # Fallback: try first gateway that has a log
    if not log_content:
        for gw_dir in GATEWAYS_DIR.iterdir():
            if not gw_dir.is_dir() or gw_dir.name.startswith("_"):
                continue
            lp = gw_dir / "state" / "workspace" / "GROUP_CHAT_LOG.md"
            if lp.exists():
                try:
                    log_content = lp.read_text(encoding="utf-8")
                    break
                except Exception:
                    continue
    topic = _arena_state.get("topic", "")
    return jsonify({"log": log_content, "topic": topic, "chars": len(log_content)})


@app.route("/api/arena/secretary/models", methods=["GET"])
def secretary_list_models():
    """Return available models for the secretary (those with API keys)."""
    from generate_persona import get_available_models
    models = get_available_models()
    return jsonify({"models": models})


@app.route("/api/arena/secretary/generate", methods=["POST"])
def secretary_generate():
    """Generate a summary using direct LLM call.

    Body: {model, prompt, include_log?: true}
    Returns: {status, result} or starts async generation.
    """
    data = request.json or {}
    model = data.get("model", "").strip()
    user_prompt = data.get("prompt", "").strip()
    include_log = data.get("include_log", True)

    if not model:
        return jsonify({"error": "请选择一个模型"}), 400
    if not user_prompt:
        return jsonify({"error": "请输入提示词"}), 400

    # Gather discussion log
    log_content = ""
    if include_log:
        herald_gw = manager.get(HERALD_GW_ID)
        if herald_gw:
            log_path = HUB_DIR / herald_gw.state_dir / "workspace" / "GROUP_CHAT_LOG.md"
            if log_path.exists():
                try:
                    log_content = log_path.read_text(encoding="utf-8")
                except Exception:
                    pass
        if not log_content:
            for gw_dir in GATEWAYS_DIR.iterdir():
                if not gw_dir.is_dir() or gw_dir.name.startswith("_"):
                    continue
                lp = gw_dir / "state" / "workspace" / "GROUP_CHAT_LOG.md"
                if lp.exists():
                    try:
                        log_content = lp.read_text(encoding="utf-8")
                        break
                    except Exception:
                        continue

    topic = _arena_state.get("topic", "未知议题")

    # Build full prompt
    full_prompt = f"""你是一位专业的讨论书记，负责根据用户的要求整理讨论记录。

## 讨论议题
{topic}

## 完整讨论记录
{log_content if log_content else "（讨论记录为空）"}

## 用户要求
{user_prompt}

请根据上述讨论记录和用户要求，输出整理后的内容。用中文回复。"""

    # Run in background thread for async polling
    _secretary_status.update({
        "status": "generating",
        "result": None,
        "error": None,
        "_ts": _time_mod.time(),
        "model": model,
    })

    def _bg_call():
        from generate_persona import _call_llm
        try:
            print(f"[secretary] Calling {model} with {len(full_prompt)} chars prompt...")
            result = _call_llm(full_prompt, model=model)
            if result:
                # Auto-save to persistent history
                try:
                    entry = _save_secretary_summary(topic, user_prompt, model, result)
                    print(f"[secretary] Saved as {entry['id']}")
                except Exception as save_err:
                    print(f"[secretary] Save failed: {save_err}")
                _secretary_status.update({
                    "status": "done",
                    "result": result,
                    "error": None,
                })
                print(f"[secretary] Done, {len(result)} chars")
            else:
                _secretary_status.update({
                    "status": "error",
                    "error": f"{model} 返回了空内容",
                })
        except Exception as e:
            print(f"[secretary] Error: {e}")
            _secretary_status.update({
                "status": "error",
                "error": str(e)[:500],
            })

    threading.Thread(target=_bg_call, daemon=True, name="secretary-gen").start()
    return jsonify({"status": "generating", "message": f"正在用 {model} 生成..."}), 202


@app.route("/api/arena/secretary/status", methods=["GET"])
def secretary_status():
    """Poll secretary generation status."""
    if not _secretary_status:
        return jsonify({"status": "idle"})
    info = {k: v for k, v in _secretary_status.items() if not k.startswith("_")}
    # Auto-clear stale generating status (> 5 min)
    if info.get("status") == "generating":
        elapsed = _time_mod.time() - _secretary_status.get("_ts", 0)
        if elapsed > 300:
            info = {"status": "error", "error": f"生成超时（{int(elapsed)}秒）"}
            _secretary_status.update(info)
    return jsonify(info)


# ── Secretary history (persistent summaries) ──────────────────

SECRETARY_DIR = HUB_DIR / "backend" / "secretary_history"
SECRETARY_INDEX = SECRETARY_DIR / "index.json"


def _read_secretary_index() -> list:
    if SECRETARY_INDEX.exists():
        try:
            return json.loads(SECRETARY_INDEX.read_text("utf-8"))
        except Exception:
            pass
    return []


def _save_secretary_index(items: list):
    SECRETARY_DIR.mkdir(parents=True, exist_ok=True)
    SECRETARY_INDEX.write_text(json.dumps(items, indent=2, ensure_ascii=False), "utf-8")


def _save_secretary_summary(topic: str, prompt: str, model: str, result: str) -> dict:
    """Persist a secretary summary to disk. Returns the saved entry metadata."""
    entry_id = uuid.uuid4().hex[:12]
    SECRETARY_DIR.mkdir(parents=True, exist_ok=True)
    md_path = SECRETARY_DIR / f"{entry_id}.md"
    md_path.write_text(result, encoding="utf-8")
    entry = {
        "id": entry_id,
        "topic": topic,
        "prompt": prompt,
        "model": model,
        "chars": len(result),
        "created_at": _time_mod.strftime("%Y-%m-%d %H:%M:%S"),
    }
    items = _read_secretary_index()
    items.insert(0, entry)
    _save_secretary_index(items)
    return entry


@app.route("/api/arena/secretary/history", methods=["GET"])
def secretary_history():
    """List all saved secretary summaries (metadata only, no full text)."""
    items = _read_secretary_index()
    return jsonify({"summaries": items})


@app.route("/api/arena/secretary/history/<entry_id>", methods=["GET"])
def secretary_history_detail(entry_id):
    """Get full text of a saved secretary summary."""
    items = _read_secretary_index()
    entry = next((e for e in items if e["id"] == entry_id), None)
    if not entry:
        return jsonify({"error": "Not found"}), 404
    md_path = SECRETARY_DIR / f"{entry_id}.md"
    text = ""
    if md_path.exists():
        try:
            text = md_path.read_text(encoding="utf-8")
        except Exception:
            pass
    return jsonify({**entry, "text": text})


@app.route("/api/arena/secretary/history/<entry_id>", methods=["DELETE"])
def secretary_history_delete(entry_id):
    """Delete a saved secretary summary."""
    items = _read_secretary_index()
    if not any(e["id"] == entry_id for e in items):
        return jsonify({"error": "Not found"}), 404
    items = [e for e in items if e["id"] != entry_id]
    _save_secretary_index(items)
    md_path = SECRETARY_DIR / f"{entry_id}.md"
    if md_path.exists():
        try:
            md_path.unlink()
        except Exception:
            pass
    return jsonify({"ok": True})


def _clear_group_chat_logs():
    """Delete GROUP_CHAT_LOG.md from all gateway workspaces."""
    count = 0
    # Scan all gateway workspace dirs directly
    for ws_dir in GATEWAYS_DIR.iterdir():
        if not ws_dir.is_dir() or ws_dir.name.startswith("_"):
            continue
        log_path = ws_dir / "state" / "workspace" / "GROUP_CHAT_LOG.md"
        if log_path.exists():
            try:
                log_path.unlink()
                count += 1
                print(f"[arena] Deleted {log_path}")
            except Exception as e:
                print(f"[arena] Failed to delete {log_path}: {e}")
    print(f"[arena] Cleared {count} GROUP_CHAT_LOG.md files")
    return count


@app.route("/api/group/clear-messages", methods=["POST"])
def clear_group_messages():
    """Clear GROUP_CHAT_LOG.md + eunuch pool."""
    count = _clear_group_chat_logs()
    try:
        pool = eunuch_get_pool()
        pool.clear(reset_collector=True)
    except Exception:
        pass
    return jsonify({"ok": True, "cleared": count})


# ── Group Management API (群管理) ─────────────────────────────
# Manage Telegram group config, participants, and messaging.

def _save_palace(palace: dict):
    """Write palace.json back to disk."""
    palace_path = HUB_DIR / "palace.json"
    palace_path.write_text(
        json.dumps(palace, indent=4, ensure_ascii=False), encoding="utf-8"
    )


def _tg_api_call(bot_token: str, method: str, params: dict = None) -> dict:
    """Call Telegram Bot API with proxy from palace.json."""
    palace = _load_palace()
    proxy = palace.get("proxy", "")
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    if params:
        data_bytes = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(url, data=data_bytes, headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    if proxy:
        handler = urllib.request.ProxyHandler({"https": proxy, "http": proxy})
        opener = urllib.request.build_opener(handler)
    else:
        opener = urllib.request.build_opener()
    try:
        with opener.open(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "description": str(e)}


@app.route("/api/group", methods=["GET"])
def get_group():
    """Get group config, participants, and their gateway status."""
    palace = _load_palace()
    tg = palace.get("telegram", {})
    participants = palace.get("participants", [])
    herald = palace.get("herald", {})

    # Enrich participants with gateway status; auto-sync names from gateways
    enriched = []
    names_dirty = False
    for p in participants:
        gw = manager.get(p["id"])
        # Auto-update stale names from current gateway
        if gw and gw.name and gw.name != p.get("name"):
            p["name"] = gw.name
            names_dirty = True
        enriched.append({
            **p,
            "botToken": "***" + p.get("botToken", "")[-6:] if p.get("botToken") else "",
            "has_token": bool(p.get("botToken")),
            "gateway_exists": gw is not None,
            "gateway_status": gw.status if gw else "no_gateway",
        })
    if names_dirty:
        _save_palace(palace)

    return jsonify({
        "groupId": tg.get("groupId", ""),
        "groupName": tg.get("groupName", ""),
        "proxy": palace.get("proxy", ""),
        "herald": {
            "id": herald.get("id", ""),
            "name": herald.get("name", ""),
            "has_token": bool(herald.get("botToken")),
        },
        "participants": enriched,
        "sync": palace.get("sync", {}),
    })


@app.route("/api/group", methods=["PATCH"])
def update_group():
    """Update group config (groupId, sync settings, etc.)."""
    data = request.json or {}
    palace = _load_palace()

    if "groupId" in data:
        palace.setdefault("telegram", {})["groupId"] = data["groupId"].strip()
    if "groupName" in data:
        palace.setdefault("telegram", {})["groupName"] = data["groupName"].strip()

    # Sync settings
    if "syncEnabled" in data:
        palace.setdefault("sync", {})["enabled"] = bool(data["syncEnabled"])
    if "syncInterval" in data:
        try:
            palace.setdefault("sync", {})["intervalSeconds"] = max(5, int(data["syncInterval"]))
        except (ValueError, TypeError):
            pass

    _save_palace(palace)
    return jsonify({"ok": True})


def _read_gateway_bot_token(gw_id: str) -> str:
    """Read bot token from a gateway's openclaw.json."""
    cfg_path = HUB_DIR / f"gateways/{gw_id}/openclaw.json"
    if not cfg_path.exists():
        return ""
    try:
        raw = cfg_path.read_text(encoding="utf-8")
        if raw and ord(raw[0]) == 0xFEFF:
            raw = raw[1:]
        cfg = json.loads(raw)
        return cfg.get("channels", {}).get("telegram", {}).get("botToken", "")
    except Exception:
        return ""


@app.route("/api/group/participants", methods=["POST"])
def add_group_participant():
    """Register a gateway as a group participant.
    Body: {id, name?, botToken?}
    If botToken is not provided, reads it from the gateway's openclaw.json.
    """
    data = request.json or {}
    pid = data.get("id", "").strip()
    if not pid:
        return jsonify({"error": "id is required"}), 400

    gw = manager.get(pid)

    # Try explicit token first, then read from openclaw.json
    bot_token = data.get("botToken", "").strip() or _read_gateway_bot_token(pid)
    if not bot_token:
        return jsonify({"error": f"Gateway {pid} 的 openclaw.json 中未找到 botToken，请先在 gateway 配置中设置"}), 400

    palace = _load_palace()
    participants = palace.setdefault("participants", [])

    name = data.get("name", "").strip() or (gw.name if gw else pid)
    bot_user_id = bot_token.split(":")[0]

    # Update existing or add new
    found = False
    for p in participants:
        if p["id"] == pid:
            p["botToken"] = bot_token
            p["botUserId"] = bot_user_id
            p["name"] = name
            found = True
            break
    if not found:
        participants.append({
            "id": pid,
            "name": name,
            "botToken": bot_token,
            "botUserId": bot_user_id,
        })

    _save_palace(palace)
    return jsonify({"ok": True, "participant": {"id": pid, "name": name}})


@app.route("/api/group/participants/<pid>", methods=["DELETE"])
def remove_group_participant(pid):
    """Remove a participant from palace.json."""
    palace = _load_palace()
    participants = palace.get("participants", [])
    palace["participants"] = [p for p in participants if p["id"] != pid]
    _save_palace(palace)
    return jsonify({"ok": True})


@app.route("/api/group/send", methods=["POST"])
def group_send_message():
    """Send a message to the Telegram group as a specific bot.
    Body: {bot_id: "herald"|"<gateway_id>", text: "message"}
    """
    data = request.json or {}
    bot_id = data.get("bot_id", "").strip()
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400

    palace = _load_palace()
    group_id = palace.get("telegram", {}).get("groupId", "")
    if not group_id:
        return jsonify({"error": "未配置群组 ID"}), 400

    # Find bot token
    bot_token = ""
    bot_name = ""
    if bot_id == "herald" or bot_id == HERALD_GW_ID:
        bot_token = palace.get("herald", {}).get("botToken", "")
        bot_name = "主持人"
    else:
        for p in palace.get("participants", []):
            if p["id"] == bot_id:
                bot_token = p.get("botToken", "")
                bot_name = p.get("name", bot_id)
                break

    if not bot_token:
        return jsonify({"error": f"未找到 {bot_id} 的 Bot Token"}), 400

    result = _tg_api_call(bot_token, "sendMessage", {
        "chat_id": group_id,
        "text": text,
    })

    if result.get("ok"):
        print(f"[group] {bot_name}({bot_id}) sent message to group: {text[:50]}")
        return jsonify({"ok": True, "message_id": result.get("result", {}).get("message_id")})
    else:
        desc = result.get("description", "Unknown error")
        return jsonify({"error": f"发送失败: {desc}"}), 502


@app.route("/api/group/messages", methods=["GET"])
def group_messages():
    """Get recent group messages from the eunuch message pool."""
    limit = request.args.get("limit", 50, type=int)
    try:
        pool = eunuch_get_pool()
        all_msgs = pool.query(types=["group"], limit=limit)
        return jsonify(all_msgs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/group/sync", methods=["POST"])
def group_trigger_sync():
    """Trigger a group sync cycle."""
    try:
        sync_once()
        return jsonify({"ok": True, "message": "同步完成"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/group/test-bot", methods=["POST"])
def group_test_bot():
    """Test a bot token by calling getMe.
    Body: {botToken: "..."}
    """
    data = request.json or {}
    token = data.get("botToken", "").strip()
    if not token:
        return jsonify({"error": "botToken is required"}), 400

    result = _tg_api_call(token, "getMe")
    if result.get("ok"):
        bot_info = result.get("result", {})
        return jsonify({
            "ok": True,
            "bot_id": str(bot_info.get("id", "")),
            "username": bot_info.get("username", ""),
            "first_name": bot_info.get("first_name", ""),
        })
    else:
        return jsonify({"error": result.get("description", "验证失败")}), 400


# ── Herald API (主持人 / Moderator) ─────────────────────────
# The herald is a real gateway so it can use openclaw CLI for Telegram.


def _write_herald_workspace(ws_dir: Path):
    """Write herald-specific workspace files (moderator persona)."""
    ws_dir.mkdir(parents=True, exist_ok=True)

    (ws_dir / "SOUL.md").write_text("""\
# SOUL.md — 主持人 / Moderator

你是**主持人**，Arena 讨论的协调者。你不是讨论参与者，不发表观点。

## 身份定位

- 你是用户的行政助手，负责在群组中传达公告、协调讨论
- 你在 Telegram 群聊中代表用户发言
- 你的语气应当**简洁、中性、实用**

## 行为准则

- **不发表观点** — 你是主持人，不是参与者
- **不站队** — 只传达、不评判
- 当用户让你在群里说话时，简洁转述
- 当用户闲聊时，简短回应
""", encoding="utf-8")

    (ws_dir / "IDENTITY.md").write_text("""\
# IDENTITY.md — 主持人

- **Name:** 主持人 / Moderator
- **Creature:** Arena 讨论协调者
- **Vibe:** 简洁、中性、实用
- **Emoji:** 🦉
""", encoding="utf-8")

    (ws_dir / "HEARTBEAT.md").write_text("""\
# 心跳任务 — 主持人

## 你是谁

你是**主持人**，Arena 讨论的协调者。你不发表观点，不参与讨论。

## 心跳回应

- 如果用户刚说了什么，简短回应
- 如果没什么事，回复：`HEARTBEAT_OK`
- **不要主动找话题闲聊，不要长篇大论**
""", encoding="utf-8")


def _provision_herald_gateway(token: str = ""):
    """Ensure the herald gateway exists. Returns the gateway object."""
    gw = manager.get(HERALD_GW_ID)
    if gw:
        return gw

    # Find next available port
    all_gws = manager.list_all()
    max_port = max((g.port for g in all_gws), default=61000)
    herald_port = max_port + 2
    if herald_port == 61000:
        herald_port += 2

    # Provision the gateway files
    try:
        provision_gateway(HERALD_GW_ID, "主持人", "🦉", herald_port)
    except Exception as e:
        print(f"[herald] provision error: {e}")
        return None

    # Overwrite workspace with herald-specific persona (moderator)
    _write_herald_workspace(HUB_DIR / f"gateways/{HERALD_GW_ID}/state/workspace")

    # Write token into openclaw.json if provided
    if token:
        cfg_path = HUB_DIR / f"gateways/{HERALD_GW_ID}/openclaw.json"
        if cfg_path.exists():
            try:
                raw = cfg_path.read_text(encoding="utf-8")
                if raw and ord(raw[0]) == 0xFEFF:
                    raw = raw[1:]
                cfg = json.loads(raw)
                cfg.setdefault("channels", {}).setdefault("telegram", {})["botToken"] = token
                cfg_path.write_text(json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

    # Register in gateways.json with role=herald
    gw_def = {
        "id": HERALD_GW_ID,
        "name": "主持人",
        "emoji": "🦉",
        "port": herald_port,
        "role": "herald",
        "config_file": f"gateways/{HERALD_GW_ID}/openclaw.json",
        "workspace_dir": f"gateways/{HERALD_GW_ID}/state/workspace",
        "state_dir": f"gateways/{HERALD_GW_ID}/state",
        "editable_files": [
            f"gateways/{HERALD_GW_ID}/openclaw.json",
        ],
    }
    gw = manager.add(gw_def)
    print(f"[herald] provisioned gateway: port={herald_port}")
    return gw


@app.route("/api/herald", methods=["GET"])
def get_herald():
    """Get herald (moderator) gateway status."""
    gw = manager.get(HERALD_GW_ID)
    if not gw:
        return jsonify({
            "provisioned": False,
            "configured": False,
            "gateway": None,
        })

    # Check if token is configured in openclaw.json
    cfg_path = HUB_DIR / gw.config_file
    cfg = _read_cfg(cfg_path)
    bot_token = cfg.get("channels", {}).get("telegram", {}).get("botToken", "")
    configured = bool(bot_token) and not bot_token.endswith("_BOT_TOKEN_HERE")

    return jsonify({
        "provisioned": True,
        "configured": configured,
        "gateway": gw.to_dict(),
        "botToken": bot_token if configured else "",
    })


@app.route("/api/herald/provision", methods=["POST"])
def provision_herald():
    """Auto-provision the herald gateway with the given token."""
    data = request.json or {}
    token = data.get("token", "").strip()
    if not token:
        return jsonify({"error": "token required"}), 400

    gw = _provision_herald_gateway(token)
    if not gw:
        return jsonify({"error": "Failed to provision herald gateway"}), 500

    # If gateway already existed, update its token
    cfg_path = HUB_DIR / gw.config_file
    if cfg_path.exists():
        try:
            raw = cfg_path.read_text(encoding="utf-8")
            if raw and ord(raw[0]) == 0xFEFF:
                raw = raw[1:]
            cfg = json.loads(raw)
            cfg.setdefault("channels", {}).setdefault("telegram", {})["botToken"] = token
            cfg_path.write_text(json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            return jsonify({"error": f"Failed to save token: {e}"}), 500

    # Also update palace.json herald section
    palace_path = HUB_DIR / "palace.json"
    try:
        palace = json.loads(palace_path.read_text(encoding="utf-8"))
        palace.setdefault("herald", {})["botToken"] = token
        palace["herald"]["name"] = "主持人"
        palace["herald"]["id"] = HERALD_GW_ID
        if ":" in token:
            palace["herald"]["botUserId"] = token.split(":")[0]
        palace_path.write_text(
            json.dumps(palace, indent=4, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass

    return jsonify({"success": True, "gateway": gw.to_dict()})


# ── Eunuch API (消息池) ──────────────────────────────────
@app.route("/api/eunuch/query", methods=["GET"])
def eunuch_query():
    agent_id = request.args.get("agent", "")
    since = request.args.get("since", None)
    limit = request.args.get("limit", 50, type=int)
    if not agent_id:
        return jsonify({"error": "agent param required"}), 400
    try:
        result = query_ext_message(agent_id, since=since, limit=limit)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/eunuch/ledger", methods=["GET"])
def eunuch_ledger():
    limit = request.args.get("limit", 5000, type=int)
    try:
        result = ledger_all(limit=limit)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/eunuch/reset", methods=["POST"])
def eunuch_reset():
    """Clear the eunuch message pool (truly empty, no re-collect)."""
    try:
        pool = eunuch_get_pool()
        pool.clear()
        return jsonify({"success": True, "message": "消息池已清空"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/eunuch/force-collect", methods=["POST"])
def eunuch_force_collect():
    """Force a collect cycle and return diagnostics."""
    from eunuch import collect_once, _read_progress
    try:
        pool = eunuch_get_pool()
        before = pool.size
        progress_before = len(_read_progress)
        collect_once(pool)
        after = pool.size
        progress_after = len(_read_progress)
        return jsonify({
            "success": True,
            "pool_before": before,
            "pool_after": after,
            "progress_files_before": progress_before,
            "progress_files_after": progress_after,
            "new_messages": after - before,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500


@app.route("/api/eunuch/rebuild", methods=["POST"])
def eunuch_rebuild():
    """Clear pool, reset collector progress, and recollect from active sessions."""
    from eunuch import _read_progress
    try:
        pool = eunuch_get_pool()
        before = pool.size
        progress_before = len(_read_progress)
        pool.clear(reset_collector=True)
        eunuch_collect_once(pool)
        after = pool.size
        progress_after = len(_read_progress)
        return jsonify({
            "success": True,
            "pool_before": before,
            "pool_after": after,
            "progress_files_before": progress_before,
            "progress_files_after": progress_after,
            "recovered_messages": after,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500


@app.route("/api/eunuch/submit", methods=["POST"])
def eunuch_submit_endpoint():
    data = request.json or {}
    agent_id = data.get("agent", "")
    actions = data.get("actions", [])
    if not agent_id:
        return jsonify({"error": "agent required"}), 400
    if not actions:
        return jsonify({"error": "actions required"}), 400
    try:
        result = eunuch_submit(agent_id, actions)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Session Compaction ────────────────────────────────────────

@app.route("/api/gateways/<gw_id>/compact-session", methods=["POST"])
def compact_session(gw_id):
    gw = manager.get(gw_id)
    if not gw:
        return jsonify({"error": "Not found"}), 404
    force = (request.json or {}).get("force", False)
    try:
        result = compact_gateway_sessions(gw_id, force=force)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Agent Thinking Stream ─────────────────────────────────────

@app.route("/api/gateways/<gw_id>/thinking", methods=["GET"])
def get_agent_thinking(gw_id):
    """
    Read the most recent N thinking blocks from the agent's active sessions.
    Returns list of {timestamp, thinking, action_after} dicts, newest first.
    """
    gw = manager.get(gw_id)
    if not gw:
        return jsonify({"error": "Not found"}), 404

    limit = request.args.get("limit", 120, type=int)
    if not limit:
        limit = 120
    limit = max(1, min(limit, 2000))

    # Find sessions dir
    gw_list = []
    try:
        gw_list = json.loads((HUB_DIR / "gateways.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    gw_cfg = next((g for g in gw_list if g.get("id") == gw_id), None)
    if not gw_cfg:
        return jsonify({"thinking": [], "is_active": False})

    sessions_dir = HUB_DIR / gw_cfg.get("state_dir", "") / "agents" / "main" / "sessions"
    if not sessions_dir.exists():
        return jsonify({"thinking": [], "is_active": False})

    # Prefer active session pointers from sessions.json (reset updates these pointers).
    # Fallback to glob scan only if sessions.json is missing/unreadable.
    from pathlib import Path
    session_files = []
    sessions_json = sessions_dir / "sessions.json"
    if sessions_json.exists():
        try:
            sessions_meta = json.loads(sessions_json.read_text(encoding="utf-8"))
            if isinstance(sessions_meta, dict):
                by_path = {}
                for sval in sessions_meta.values():
                    if not isinstance(sval, dict):
                        continue

                    cand = None
                    session_file = sval.get("sessionFile")
                    session_id = sval.get("sessionId")

                    if isinstance(session_file, str) and session_file.strip():
                        p = Path(session_file)
                        if not p.is_absolute():
                            p = sessions_dir / session_file
                        cand = p
                    elif isinstance(session_id, str) and session_id.strip():
                        cand = sessions_dir / f"{session_id}.jsonl"

                    if cand and cand.exists():
                        by_path[str(cand)] = cand

                session_files = sorted(by_path.values(), key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            session_files = []

    if not session_files:
        session_files = sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not session_files:
        return jsonify({"thinking": [], "is_active": False})

    latest = session_files[0]
    # "active" = modified within last 5 minutes
    import time
    is_active = (time.time() - latest.stat().st_mtime) < 300

    # Helper: parse intents/actions from a write tool call's content arg
    def _parse_intents(write_content_str):
        """Try to extract intent list from JSON written to INTENTS/ACTIONS file."""
        try:
            data = json.loads(write_content_str)
            if isinstance(data, list):
                return data
        except Exception:
            pass
        # Try to find JSON array inside the string
        import re
        m = re.search(r'\[.*\]', write_content_str, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        return None

    def _semantic_action(tool_calls, text_blocks, next_tool_result):
        """Build semantic action_label + action_detail from tool calls."""
        if not tool_calls and not text_blocks:
            return None

        if text_blocks and not tool_calls:
            combined = " ".join(t.get("text", "") for t in text_blocks)
            return {
                "type": "reply",
                "label": "回复",
                "text": combined[:200],
                "tools": [],
                "args": [],
                "action_detail": None,
                "tool_result_preview": None,
            }

        # tool calls present
        result = {
            "type": "tool",
            "label": "",
            "text": None,
            "tools": [],
            "args": [],
            "action_detail": None,
            "tool_result_preview": next_tool_result[:120] if next_tool_result else None,
        }

        labels = []
        for tc in tool_calls:
            name = tc.get("name", "")
            args = tc.get("arguments", {}) or {}
            result["tools"].append(name)

            if name == "exec":
                cmd = args.get("command", "")
                result["args"].append(str(cmd)[:80])
                # Semantic label for known scripts
                if "query_eunuch" in cmd:
                    labels.append("查询消息池")
                elif "submit_intents" in cmd or "submit_actions" in cmd:
                    labels.append("提交行动")
                elif "query_game_state" in cmd:
                    labels.append("查询状态")
                else:
                    labels.append(f"执行 {cmd[:40]}")

            elif name == "write":
                filepath = args.get("path", args.get("file", ""))
                content_str = args.get("content", "")
                fname = filepath.split("/")[-1].split("\\")[-1] if filepath else ""
                result["args"].append(fname[:40])

                # Try to parse intents if writing INTENTS/ACTIONS file
                if any(k in fname.upper() for k in ("INTENT", "ACTION")):
                    parsed = _parse_intents(content_str)
                    if parsed:
                        # Extract _decree_plan annotation if present
                        decree_plan = None
                        real_intents = []
                        for item in parsed:
                            if isinstance(item, dict) and "_decree_plan" in item:
                                decree_plan = item["_decree_plan"]
                            else:
                                real_intents.append(item)
                        result["action_detail"] = real_intents if real_intents else parsed
                        result["decree_plan"] = decree_plan
                        labels.append(f"写入 {len(real_intents)} 条行动意图")
                    else:
                        labels.append(f"写入 {fname}")
                else:
                    labels.append(f"写入 {fname}")

            elif name == "read":
                filepath = args.get("path", args.get("file", ""))
                fname = filepath.split("/")[-1].split("\\")[-1] if filepath else filepath
                labels.append(f"读取 {fname[:30]}")
                result["args"].append(fname[:40])

            else:
                labels.append(name)

        result["label"] = " · ".join(labels) if labels else "执行工具"

        # If there are also text blocks alongside tool calls, capture them
        if text_blocks:
            combined = " ".join(t.get("text", "") for t in text_blocks)
            result["text"] = combined[:200]

        return result

    # Parse sessions: collect messages in chronological order across active session files.
    all_messages = []
    session_files_chrono = sorted(session_files, key=lambda p: p.stat().st_mtime)
    for sf in session_files_chrono:
        try:
            with open(sf, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    if entry.get("type") != "message":
                        continue
                    all_messages.append(entry)
        except Exception:
            continue

    # Build results: for each assistant message, extract thinking blocks or text fallback
    results = []
    for i, entry in enumerate(all_messages):
        msg = entry.get("message", {})
        if msg.get("role") != "assistant":
            continue

        content = msg.get("content", [])
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        thinking_blocks = [c for c in content if c.get("type") == "thinking"]
        tool_calls = [c for c in content if c.get("type") == "toolCall"]
        text_blocks = [c for c in content if c.get("type") == "text"]

        # Find the immediately following toolResult message(s)
        next_tool_result = ""
        for j in range(i + 1, min(i + 3, len(all_messages))):
            next_msg = all_messages[j].get("message", {})
            next_role = next_msg.get("role", "")
            if next_role in ("toolResult", "tool"):
                nc = next_msg.get("content", [])
                if isinstance(nc, list):
                    for c in nc:
                        txt = c.get("text", "") if isinstance(c, dict) else str(c)
                        next_tool_result += txt[:200]
                elif isinstance(nc, str):
                    next_tool_result = nc[:200]
                break
            elif next_role == "user":
                nc = next_msg.get("content", [])
                if isinstance(nc, list):
                    tr_blocks = [c for c in nc if isinstance(c, dict) and c.get("type") == "toolResult"]
                    if tr_blocks:
                        for trb in tr_blocks[:1]:
                            trc = trb.get("content", "")
                            if isinstance(trc, list):
                                next_tool_result = " ".join(x.get("text", "") for x in trc)[:200]
                            else:
                                next_tool_result = str(trc)[:200]
                        break
                break

        action = _semantic_action(tool_calls, text_blocks, next_tool_result)
        ts = entry.get("timestamp", "")

        if thinking_blocks:
            for tb in thinking_blocks:
                results.append({
                    "timestamp": ts,
                    "thinking": tb.get("thinking", ""),
                    "action_after": action,
                })
        else:
            # Fallback: show assistant text replies for models without thinking support
            text = "\n".join(b.get("text", "") for b in text_blocks).strip()
            if text and text not in ("HEARTBEAT_OK", "NO_REPLY"):
                results.append({
                    "timestamp": ts,
                    "thinking": text,
                    "action_after": action,
                    "is_reply": True,
                })

    # Return newest-first, limited
    results.sort(key=lambda r: r.get("timestamp", ""))
    if len(results) > limit:
        results = results[-limit:]
    results.reverse()

    return jsonify({
        "thinking": results,
        "is_active": is_active,
        "session": latest.name,
        "sessions_scanned": len(session_files_chrono),
    })


# ── Model Stats ───────────────────────────────────────────────

@app.route("/api/model-stats", methods=["GET"])
def model_stats():
    from pathlib import Path
    stats = {}  # key: "provider/model" -> {calls, input, output, cacheRead, cacheWrite, totalTokens, cost}
    gateways_json = HUB_DIR / "gateways.json"
    try:
        gw_list = json.loads(gateways_json.read_text(encoding="utf-8"))
    except Exception:
        gw_list = []

    for gw in gw_list:
        sessions_dir = HUB_DIR / gw.get("state_dir", "") / "agents" / "main" / "sessions"
        if not sessions_dir.exists():
            continue
        for jf in sessions_dir.glob("*.jsonl"):
            try:
                with open(jf, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except Exception:
                            continue
                        if entry.get("type") != "message":
                            continue
                        msg = entry.get("message", {})
                        if msg.get("role") != "assistant":
                            continue
                        provider = msg.get("provider", "")
                        model = msg.get("model", "")
                        usage = msg.get("usage", {})
                        if not provider or not model or not usage:
                            continue
                        key = f"{provider}/{model}"
                        if key not in stats:
                            stats[key] = {"model": key, "provider": provider,
                                          "calls": 0, "input": 0, "output": 0,
                                          "cacheRead": 0, "cacheWrite": 0,
                                          "totalTokens": 0, "cost": 0.0}
                        s = stats[key]
                        s["calls"] += 1
                        s["input"] += usage.get("input", 0)
                        s["output"] += usage.get("output", 0)
                        s["cacheRead"] += usage.get("cacheRead", 0)
                        s["cacheWrite"] += usage.get("cacheWrite", 0)
                        s["totalTokens"] += usage.get("totalTokens", 0)
                        cost = usage.get("cost", {})
                        if isinstance(cost, dict):
                            s["cost"] += cost.get("total", 0.0)
                        elif isinstance(cost, (int, float)):
                            s["cost"] += cost
            except Exception:
                continue

    result = sorted(stats.values(), key=lambda x: x["cost"], reverse=True)
    total_cost = sum(s["cost"] for s in result)
    total_calls = sum(s["calls"] for s in result)
    total_tokens = sum(s["totalTokens"] for s in result)
    return jsonify({
        "models": result,
        "total_cost": total_cost,
        "total_calls": total_calls,
        "total_tokens": total_tokens,
    })


# ── Session Hard Reset (Nuke) ─────────────────────────────────

def _nuke_gateway_sessions(gw_id: str) -> dict:
    """Reset a single gateway's sessions: create new empty .jsonl files,
    update sessions.json pointers.  Old files are kept intact.
    Also clears dynamic workspace files.
    Returns a summary dict.  Gateway must be stopped by caller."""
    import uuid as _uuid

    result = {"id": gw_id, "sessions_reset": 0}
    sessions_dir = GATEWAYS_DIR / gw_id / "state" / "agents" / "main" / "sessions"
    sessions_json_path = sessions_dir / "sessions.json"

    if not sessions_json_path.exists():
        result["skipped"] = "sessions.json not found"
        return result

    try:
        data = json.loads(sessions_json_path.read_text(encoding="utf-8"))
    except Exception as e:
        result["error"] = str(e)
        return result

    # Create new empty .jsonl, update sessions.json pointers
    reset_count = 0
    for skey, sval in data.items():
        if not isinstance(sval, dict) or "sessionId" not in sval:
            continue
        new_id = str(_uuid.uuid4())
        new_file = sessions_dir / f"{new_id}.jsonl"
        new_file.write_text("", encoding="utf-8")
        old_id = sval["sessionId"]
        sval["sessionId"] = new_id
        sval["sessionFile"] = str(new_file)
        for counter_key in ("inputTokens", "outputTokens", "totalTokens"):
            if counter_key in sval:
                sval[counter_key] = 0
        if "lastHeartbeatText" in sval:
            sval["lastHeartbeatText"] = ""
        reset_count += 1
        print(f"[reset] {gw_id}: session [{skey}] {old_id[:8]}→{new_id[:8]}")

    sessions_json_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    result["sessions_reset"] = reset_count

    # 4) Clear dynamic workspace files
    ws_dir = GATEWAYS_DIR / gw_id / "state" / "workspace"
    for fname in ("GROUP_CHAT_LOG.md",):
        fp = ws_dir / fname
        if fp.exists():
            fp.write_text("", encoding="utf-8")

    # 5) Reset MEMORY.md: keep header, clear entries
    mem_fp = ws_dir / "MEMORY.md"
    if mem_fp.exists():
        try:
            mem_lines = mem_fp.read_text(encoding="utf-8").splitlines()
            header_lines = []
            for line in mem_lines:
                header_lines.append(line)
                if line.strip() == "## 记忆条目":
                    break
            mem_fp.write_text("\n".join(header_lines) + "\n", encoding="utf-8")
        except Exception:
            pass

    # 6) Clear memory archive (contains old meta-language that pollutes new sessions)
    archive_dir = ws_dir / "memory" / "archive"
    if archive_dir.exists():
        cleared = 0
        for f in archive_dir.iterdir():
            if f.suffix == ".md":
                try:
                    f.write_text("", encoding="utf-8")
                    cleared += 1
                except Exception:
                    pass
        if cleared:
            print(f"[reset] {gw_id}: 清空 {cleared} 个记忆存档")

    return result


@app.route("/api/gateways/<gw_id>/reset-session", methods=["POST"])
def reset_gateway_session(gw_id):
    """Hard-reset a single gateway's sessions (stop → nuke → restart)."""
    import time as _time
    gw = manager.get(gw_id)
    if not gw:
        return jsonify({"error": "Not found"}), 404
    try:
        gw.stop()
        _time.sleep(1)
        result = _nuke_gateway_sessions(gw_id)
        _time.sleep(0.5)
        gw.start()
        result["restarted"] = True
        print(f"[reset] {gw_id}: 心流重置完成，重建 {result['sessions_reset']} 个会话")
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/gateways/reset-all-sessions", methods=["POST"])
def reset_all_gateway_sessions():
    """Hard-reset ALL gateway sessions (stop all → nuke all → restart all)."""
    import time as _time
    gw_list = list(manager.gateways.values())
    if not gw_list:
        return jsonify({"error": "No gateways"}), 400

    # Stop all
    for gw in gw_list:
        try:
            gw.stop()
        except Exception:
            pass
    _time.sleep(2)

    # Nuke all
    results = []
    for gw in gw_list:
        results.append(_nuke_gateway_sessions(gw.id))

    # Restart all
    _time.sleep(1)
    for gw in gw_list:
        try:
            gw.start()
        except Exception:
            pass

    total_sessions = sum(r.get("sessions_reset", 0) for r in results)
    print(f"[reset] 全局心流重置：{len(results)} 个 gateway，重建 {total_sessions} 个 session")
    return jsonify({"results": results, "total_sessions_reset": total_sessions})


if __name__ == "__main__":
    start_eunuch()
    print("OpenClaw Hub Manager running at http://127.0.0.1:61000")
    app.run(host="127.0.0.1", port=61000, debug=False, threaded=True)
