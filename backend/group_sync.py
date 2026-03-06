"""
group_sync.py — 群组消息同步服务

读取所有 gateway 的 session jsonl 文件，合并群聊消息时间线，
写入每个 gateway workspace 的 GROUP_CHAT_LOG.md。

同时扫描 assistant 回复中的 [DM:owner] 和 [WHISPER:id] 标记，
将内容路由到对应目标。
"""

import json
import os
import re
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

HUB_DIR = Path(__file__).resolve().parent.parent

# ── Helpers ───────────────────────────────────────────────────

def _read_json(path: Path) -> dict | list:
    try:
        raw = path.read_text(encoding="utf-8")
        if raw and ord(raw[0]) == 0xFEFF:
            raw = raw[1:]
        return json.loads(raw)
    except Exception:
        return {}


def _load_palace() -> dict:
    return _read_json(HUB_DIR / "palace.json")


def _load_gateways() -> list[dict]:
    data = _read_json(HUB_DIR / "gateways.json")
    return data if isinstance(data, list) else []


# ── Session JSONL parser ─────────────────────────────────────

def _find_group_session_file(state_dir: Path, group_id: str) -> Path | None:
    """Find the session file for a specific telegram group."""
    sessions_json = state_dir / "agents" / "main" / "sessions" / "sessions.json"
    if not sessions_json.exists():
        return None
    sessions = _read_json(sessions_json)
    # Look for session key matching the group
    for key, info in sessions.items():
        if isinstance(info, dict) and info.get("groupId") == group_id:
            sf = info.get("sessionFile")
            if sf:
                p = Path(sf)
                if p.exists():
                    return p
    return None


def _extract_messages(session_file: Path, max_messages: int = 30) -> list[dict]:
    """
    Extract recent user+assistant messages from a session jsonl file.
    Returns list of {timestamp, role, sender, text}.
    """
    messages = []
    try:
        with open(session_file, "r", encoding="utf-8") as f:
            for line in f:
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
                role = msg.get("role")
                if role not in ("user", "assistant"):
                    continue
                ts = entry.get("timestamp", "")
                # Extract text content
                content_parts = msg.get("content", [])
                if isinstance(content_parts, str):
                    text = content_parts
                elif isinstance(content_parts, list):
                    text_parts = []
                    for part in content_parts:
                        if isinstance(part, dict) and part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                    text = "\n".join(text_parts).strip()
                else:
                    text = ""
                if not text:
                    continue
                # Parse sender from user messages
                sender = ""
                if role == "user":
                    sender = _extract_sender(text)
                messages.append({
                    "timestamp": ts,
                    "role": role,
                    "sender": sender,
                    "text": text,
                })
    except Exception:
        pass
    # Return only the last N messages
    return messages[-max_messages:] if len(messages) > max_messages else messages


def _extract_sender(text: str) -> str:
    """Extract sender name from openclaw's user message metadata."""
    # Pattern: "label": "Name" or "name": "Name" in Sender metadata
    m = re.search(r'Sender \(untrusted metadata\):\s*```json\s*\{[^}]*"name"\s*:\s*"([^"]+)"', text, re.DOTALL)
    if m:
        return m.group(1)
    return ""


def _extract_user_text(text: str) -> str:
    """Strip openclaw metadata from user message, return just the user's actual text."""
    # Skip heartbeat prompts entirely
    if "HEARTBEAT" in text or "Read HEARTBEAT.md" in text or "HEARTBEAT_OK" in text:
        return ""
    # The actual user text comes after the metadata blocks
    # Pattern: everything after the last ``` block
    parts = text.split("```")
    if len(parts) >= 3:
        # Last part after closing ``` of metadata
        remainder = parts[-1].strip()
        if remainder:
            return remainder
    return text


def _clean_assistant_text(text: str) -> str:
    """Remove leading whitespace/newlines and stray tags from assistant text."""
    text = text.strip()
    # Remove <final>...</final> wrapper if present
    text = re.sub(r'</?final>', '', text).strip()
    return text


# ── Tag parsing ──────────────────────────────────────────────

TAG_DM = re.compile(r'\[DM:(\w+)\]\s*(.*?)(?=\[(?:DM|WHISPER):|\Z)', re.DOTALL)
TAG_WHISPER = re.compile(r'\[WHISPER:(\w+)\]\s*(.*?)(?=\[(?:DM|WHISPER):|\Z)', re.DOTALL)


def _parse_tags(text: str, palace: dict, gateways: list[dict]) -> tuple[str, list[dict]]:
    """
    Parse [DM:owner] and [WHISPER:id] tags from assistant text.
    Returns (clean_text_for_group, list_of_actions).
    Actions: [{"type": "dm"|"whisper", "target_id": str, "content": str}]
    """
    actions = []
    clean = text

    # Build name→id lookup from palace + gateways
    name_to_id = {}
    owner = palace.get("owner", {})
    owner_id = owner.get("id", "owner")
    name_to_id[owner_id] = owner_id
    for alias in owner.get("aliases", []):
        name_to_id[alias.lower()] = owner_id
    name_to_id[owner.get("name", "").lower()] = owner_id

    for gw in gateways:
        gw_id = gw.get("id", "")
        name_to_id[gw_id] = gw_id
        name_to_id[gw.get("name", "").lower()] = gw_id

    # Extract DM tags
    for m in TAG_DM.finditer(text):
        raw_target = m.group(1).strip()
        content = m.group(2).strip()
        target_id = name_to_id.get(raw_target.lower(), raw_target.lower())
        if content:
            actions.append({"type": "dm", "target_id": target_id, "content": content})
        clean = clean.replace(m.group(0), "")

    # Extract WHISPER tags
    for m in TAG_WHISPER.finditer(text):
        raw_target = m.group(1).strip()
        content = m.group(2).strip()
        target_id = name_to_id.get(raw_target.lower(), raw_target.lower())
        if content:
            actions.append({"type": "whisper", "target_id": target_id, "content": content})
        clean = clean.replace(m.group(0), "")

    return clean.strip(), actions


# ── Whisper file writer ──────────────────────────────────────

def _append_whisper(target_gw: dict, from_name: str, content: str):
    """Append a whisper entry to target gateway's WHISPERS.md."""
    workspace = HUB_DIR / target_gw.get("workspace_dir", "")
    whisper_file = workspace / "WHISPERS.md"
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
    entry = f"\n[{now}] {from_name} 私下对你说: {content}\n"
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        with open(whisper_file, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception:
        pass


# ── GROUP_CHAT_LOG.md generator ──────────────────────────────

def _format_time(ts_str: str) -> str:
    """Convert ISO timestamp to HH:MM (Asia/Shanghai)."""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        dt_local = dt + timedelta(hours=8)
        return dt_local.strftime("%H:%M")
    except Exception:
        return "??:??"


def _build_group_chat_log(
    all_messages: list[dict],
    palace: dict,
    gateways: list[dict],
) -> str:
    """Build the GROUP_CHAT_LOG.md content."""
    lines = []

    lines.append("# 群组讨论记录")
    lines.append("")
    lines.append("以下是群组中最近的对话。你可以看到所有参与者的发言。")
    lines.append("")
    lines.append("## 私下消息（用 write 工具写文件，不要写在回复里！）")
    owner = palace.get("owner", {})
    lines.append(f"- 私信给{owner.get('name', '用户')}：write 追加到 `DM_OUTBOX.md`，格式 `[给{owner.get('name', '用户')}] 内容`")
    for gw in gateways:
        lines.append(f"- 私信给{gw.get('name', gw['id'])}：write 追加到 `WHISPER_OUTBOX.md`，格式 `[给{gw.get('name', gw['id'])}] 内容`")
    lines.append("")
    lines.append("⚠️ 你的最终回复会直接发到群里！私下的消息必须写到上面的文件里，不要放在回复中。")
    lines.append("")
    lines.append("## 最近对话")
    lines.append("")

    if not all_messages:
        lines.append("（暂无消息）")
        return "\n".join(lines)

    for msg in all_messages:
        t = _format_time(msg["timestamp"])
        role = msg["role"]
        sender = msg.get("sender_display", "")
        text = msg.get("display_text", "")

        if not text:
            continue

        # Truncate very long messages
        if len(text) > 200:
            text = text[:200] + "..."

        # Single-line format
        text_oneline = text.replace("\n", " ").strip()
        lines.append(f"[{t}] {sender}: {text_oneline}")

    lines.append("")
    return "\n".join(lines)


# ── Telegram Bot API ─────────────────────────────────────────

def _tg_api(bot_token: str, method: str, params: dict = None, proxy: str = None) -> dict:
    """Call Telegram Bot API."""
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    if params:
        data = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
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
        print(f"[group_sync] TG API error ({method}): {e}")
        return {"ok": False, "description": str(e)}


# ── Outbox processing ────────────────────────────────────────

_DM_RE = re.compile(r'\[给(.+?)\]\s*(.*?)(?=\[给|\Z)', re.DOTALL)


def _process_outboxes(palace: dict, gateways: list[dict]):
    """Read DM_OUTBOX.md and WHISPER_OUTBOX.md from each gateway, route content, clear files."""
    proxy = palace.get("proxy", "")
    owner_cfg = palace.get("owner", {})
    owner_chat_id = owner_cfg.get("telegramUserId", "")
    owner_name = owner_cfg.get("name", "用户")
    participants = palace.get("participants", [])

    # Build name → gateway lookup
    name_to_gw = {}
    for gw in gateways:
        name_to_gw[gw.get("name", "").lower()] = gw
        name_to_gw[gw["id"].lower()] = gw

    # Build id → bot token lookup
    id_to_token = {}
    for c in participants:
        id_to_token[c["id"]] = c.get("botToken", "")

    for gw in gateways:
        workspace = HUB_DIR / gw.get("workspace_dir", "")
        sender_name = gw.get("name", gw["id"])
        sender_token = id_to_token.get(gw["id"], "")

        # Process DM_OUTBOX.md
        dm_outbox = workspace / "DM_OUTBOX.md"
        if dm_outbox.exists():
            try:
                content = dm_outbox.read_text(encoding="utf-8").strip()
                if content:
                    # Parse entries
                    for m in _DM_RE.finditer(content):
                        target_name = m.group(1).strip()
                        msg_content = m.group(2).strip()
                        if not msg_content:
                            continue
                        # Send DM to owner
                        if owner_chat_id and sender_token:
                            result = _tg_api(sender_token, "sendMessage", {
                                "chat_id": owner_chat_id,
                                "text": f"💌 {sender_name}私信你说:\n\n{msg_content}",
                            }, proxy)
                            if result.get("ok"):
                                print(f"[group_sync] DM sent: {sender_name} -> {owner_name}")
                            else:
                                print(f"[group_sync] DM failed: {result.get('description', '?')}")
                    # Clear the file
                    dm_outbox.write_text("", encoding="utf-8")
            except Exception as e:
                print(f"[group_sync] DM outbox error ({gw['id']}): {e}")

        # Process WHISPER_OUTBOX.md
        whisper_outbox = workspace / "WHISPER_OUTBOX.md"
        if whisper_outbox.exists():
            try:
                content = whisper_outbox.read_text(encoding="utf-8").strip()
                if content:
                    for m in _DM_RE.finditer(content):
                        target_name = m.group(1).strip()
                        msg_content = m.group(2).strip()
                        if not msg_content:
                            continue
                        # Find target gateway
                        target_gw = name_to_gw.get(target_name.lower())
                        if target_gw and target_gw["id"] != gw["id"]:
                            _append_whisper(target_gw, sender_name, msg_content)
                            print(f"[group_sync] whisper: {sender_name} -> {target_gw['id']}")
                    # Clear the file
                    whisper_outbox.write_text("", encoding="utf-8")
            except Exception as e:
                print(f"[group_sync] whisper outbox error ({gw['id']}): {e}")


# ── Core sync logic ──────────────────────────────────────────

def sync_once():
    """Run one sync cycle: read all sessions, merge, write GROUP_CHAT_LOG.md."""
    palace = _load_palace()
    if not palace.get("sync", {}).get("enabled"):
        return

    gateways = _load_gateways()
    if not gateways:
        return

    group_id = palace.get("telegram", {}).get("groupId", "")
    if not group_id:
        return

    sync_cfg = palace.get("sync", {})
    group_cfg = sync_cfg.get("groupChat", {})
    max_messages = group_cfg.get("maxMessages", 30)
    log_filename = group_cfg.get("file", "GROUP_CHAT_LOG.md")

    # Collect messages from all gateways
    all_messages = []
    gw_by_id = {gw["id"]: gw for gw in gateways}

    for gw in gateways:
        state_dir = HUB_DIR / gw.get("state_dir", "")
        session_file = _find_group_session_file(state_dir, group_id)
        if not session_file:
            continue

        messages = _extract_messages(session_file, max_messages * 2)
        for msg in messages:
            if msg["role"] == "user":
                user_text = _extract_user_text(msg["text"])
                if not user_text:
                    continue
                sender = msg.get("sender", "")
                # Map sender to owner display name if it matches owner's telegram info
                owner_cfg = palace.get("owner", {})
                owner_name = owner_cfg.get("name", "用户")
                owner_aliases = [a.lower() for a in owner_cfg.get("aliases", [])]
                if not sender or sender.lower() in owner_aliases or sender == "Anonymous":
                    sender = owner_name
                msg["sender_display"] = sender
                msg["display_text"] = user_text
                msg["source_gw"] = gw["id"]
            elif msg["role"] == "assistant":
                text = _clean_assistant_text(msg["text"])
                if not text or text in ("NO_REPLY", "HEARTBEAT_OK"):
                    continue
                # Parse tags
                clean_text, actions = _parse_tags(text, palace, gateways)
                msg["display_text"] = clean_text
                msg["sender_display"] = gw.get("name", gw["id"])
                msg["source_gw"] = gw["id"]
                msg["actions"] = actions

                # Execute whisper actions
                for action in actions:
                    if action["type"] == "whisper":
                        target_gw = gw_by_id.get(action["target_id"])
                        if target_gw and target_gw["id"] != gw["id"]:
                            _append_whisper(
                                target_gw,
                                gw.get("name", gw["id"]),
                                action["content"],
                            )
            all_messages.append(msg)

    # Sort by timestamp, deduplicate
    all_messages.sort(key=lambda m: m.get("timestamp", ""))

    # Deduplicate: same user text from different gateways (both bots see user msgs)
    seen = set()
    deduped = []
    for msg in all_messages:
        # Key: role + first 80 chars of display_text + timestamp (minute precision)
        text_key = (msg.get("display_text", "") or "")[:80]
        ts_minute = msg.get("timestamp", "")[:16]  # YYYY-MM-DDTHH:MM
        dedup_key = (msg["role"], msg.get("source_gw", ""), text_key, ts_minute)
        if msg["role"] == "user":
            # For user messages, deduplicate across gateways (same user msg seen by both bots)
            dedup_key = ("user", text_key, ts_minute)
        if dedup_key not in seen:
            seen.add(dedup_key)
            deduped.append(msg)

    # Take last N
    deduped = deduped[-max_messages:]

    # Build log content
    log_content = _build_group_chat_log(deduped, palace, gateways)

    # Write to each gateway's workspace
    for gw in gateways:
        workspace = HUB_DIR / gw.get("workspace_dir", "")
        log_path = workspace / log_filename
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            log_path.write_text(log_content, encoding="utf-8")
        except Exception:
            pass

    # Process outboxes (DM and whisper routing)
    try:
        _process_outboxes(palace, gateways)
    except Exception as e:
        print(f"[group_sync] outbox processing error: {e}")


# ── Background thread ────────────────────────────────────────

_sync_thread = None
_sync_stop = threading.Event()


def start_sync():
    """Start the background sync thread."""
    global _sync_thread
    if _sync_thread and _sync_thread.is_alive():
        return

    palace = _load_palace()
    interval = palace.get("sync", {}).get("intervalSeconds", 30)

    _sync_stop.clear()

    def _loop():
        while not _sync_stop.is_set():
            try:
                sync_once()
            except Exception as e:
                print(f"[group_sync] error: {e}")
            _sync_stop.wait(interval)

    _sync_thread = threading.Thread(target=_loop, daemon=True, name="group_sync")
    _sync_thread.start()
    print(f"[group_sync] started (interval={interval}s)")


def stop_sync():
    """Stop the background sync thread."""
    _sync_stop.set()
    if _sync_thread:
        _sync_thread.join(timeout=5)
    print("[group_sync] stopped")
