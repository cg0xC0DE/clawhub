"""
eunuch.py — 消息池 (Message Pool)

全局消息池：以最高频从各 gateway 的 session jsonl 收集消息，
统一排重、索引。提供 queryExtMessage / submitActions 接口。
"""

import hashlib
import json
import re
import threading
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HUB_DIR = Path(__file__).resolve().parent.parent
POOL_FILE = HUB_DIR / "backend" / "eunuch_pool.jsonl"

# ── Helpers ───────────────────────────────────────────────────


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_palace() -> dict:
    return _read_json(HUB_DIR / "palace.json")


def _load_gateways() -> list[dict]:
    data = _read_json(HUB_DIR / "gateways.json")
    return data if isinstance(data, list) else []


def _content_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:8]


def _tg_api(bot_token: str, method: str, params: dict = None,
            proxy: str = None) -> dict:
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    if params:
        data = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    if proxy:
        handler = urllib.request.ProxyHandler(
            {"https": proxy, "http": proxy})
        opener = urllib.request.build_opener(handler)
    else:
        opener = urllib.request.build_opener()
    try:
        with opener.open(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[eunuch] TG API error ({method}): {e}")
        return {"ok": False, "description": str(e)}


# ── Message Pool ──────────────────────────────────────────────


class MessagePool:
    """Thread-safe in-memory message pool with dedup and persistence."""

    def __init__(self, pool_file: Path, max_size: int = 10000):
        self._pool_file = pool_file
        self._max_size = max_size
        self._messages: list[dict] = []
        self._ids: set[str] = set()
        self._lock = threading.Lock()
        self._dirty = False
        self._load()

    def _load(self):
        if not self._pool_file.exists():
            return
        try:
            with open(self._pool_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    msg = json.loads(line)
                    mid = msg.get("id", "")
                    if mid and mid not in self._ids:
                        self._messages.append(msg)
                        self._ids.add(mid)
        except Exception as e:
            print(f"[eunuch] pool load error: {e}")
        self._messages.sort(key=lambda m: m.get("timestamp", ""))
        self._trim()

    def _trim(self):
        while len(self._messages) > self._max_size:
            removed = self._messages.pop(0)
            self._ids.discard(removed.get("id", ""))

    def add(self, msg: dict) -> bool:
        with self._lock:
            mid = msg.get("id", "")
            if mid and mid in self._ids:
                return False
            self._ids.add(mid)
            self._messages.append(msg)
            self._dirty = True
            self._trim()
            return True

    def query(self, types: list[str] = None, target: str = None,
              since: str = None, limit: int = 50) -> list[dict]:
        with self._lock:
            result = []
            for msg in reversed(self._messages):
                if since and msg.get("timestamp", "") <= since:
                    break
                if types and msg.get("type") not in types:
                    continue
                if target:
                    # group & decree are visible to everyone; others need target match
                    if msg.get("type") not in ("group", "decree"):
                        if msg.get("target") != target:
                            continue
                result.append(msg)
                if len(result) >= limit:
                    break
            result.reverse()
            return result

    def persist(self):
        with self._lock:
            if not self._dirty:
                return
            try:
                self._pool_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self._pool_file, "w", encoding="utf-8") as f:
                    for msg in self._messages:
                        f.write(json.dumps(msg, ensure_ascii=False) + "\n")
                self._dirty = False
            except Exception as e:
                print(f"[eunuch] pool persist error: {e}")

    @property
    def size(self):
        return len(self._messages)

    def get_all(self, limit: int = 5000) -> list[dict]:
        """Return all messages (including eunuch_log) for the ledger view."""
        with self._lock:
            return list(self._messages[-limit:])

    def clear(self, reset_collector: bool = False):
        """Clear all messages and persist. Optionally reset collector progress."""
        with self._lock:
            self._messages.clear()
            self._ids.clear()
            self._dirty = True
        self.persist()
        if reset_collector:
            reset_read_progress()

    def count_by_type(self) -> dict:
        with self._lock:
            counts = {}
            for m in self._messages:
                t = m.get("type", "unknown")
                counts[t] = counts.get(t, 0) + 1
            return counts


# ── Eunuch Ledger Logging ─────────────────────────────

def log_action(pool: 'MessagePool', action: str, detail: str = "",
               related: str = ""):
    """Record an eunuch action into the pool as type='eunuch_log'.
    These entries are visible in the ledger but never leaked via queryExtMessage."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    ch = _content_hash(f"{action}{detail}{now}")
    pool.add({
        "id": f"elog.{ch}.{now}",
        "type": "eunuch_log",
        "route": "",
        "sender": "消息池",
        "sender_display": "消息池",
        "target": related,
        "timestamp": now,
        "content": f"[{action}] {detail}" if detail else f"[{action}]",
        "verified": "truth",
        "source_gw": "eunuch",
    })


# ── JSONL Parsing ─────────────────────────────────────────────

_META_RE = re.compile(
    r'(?:Conversation info|Sender)\s*\(untrusted metadata\):\s*```json\s*(\{.*?\})\s*```',
    re.DOTALL,
)


def _parse_user_message(text: str) -> dict | None:
    """Parse user message text → {conv_info, sender_info, text} or None."""
    if "HEARTBEAT" in text and "Current time:" in text:
        return None

    conv_info = {}
    sender_info = {}
    for block_str in _META_RE.findall(text):
        try:
            obj = json.loads(block_str)
        except json.JSONDecodeError:
            continue
        if "conversation_label" in obj or "group_subject" in obj:
            conv_info = obj
        elif "label" in obj or "name" in obj:
            sender_info = obj

    actual = _META_RE.sub("", text).strip()
    if not actual:
        return None

    return {"conv_info": conv_info, "sender_info": sender_info, "text": actual}


def _parse_assistant_text(content_list: list) -> str:
    parts = []
    for item in content_list:
        if isinstance(item, dict) and item.get("type") == "text":
            t = item.get("text", "").strip()
            if t:
                parts.append(t)
    text = "\n".join(parts)
    text = re.sub(r"</?final>", "", text).strip()
    return text


def _resolve_sender(sender_info: dict, palace: dict):
    """Return (sender_id, sender_display)."""
    name_raw = sender_info.get("name", "") or sender_info.get("label", "")

    owner_cfg = palace.get("owner", {})
    owner_name = owner_cfg.get("name", "用户")
    owner_aliases = {a.lower() for a in owner_cfg.get("aliases", [])}

    # Telegram anonymous admin sends as "Anonymous", "GroupAnonymousBot", "Group", etc.
    _ANON_NAMES = {"anonymous", "groupanonymousbot", "group", "anonymous admin"}
    if (not name_raw
            or name_raw.lower() in owner_aliases
            or name_raw.lower() in _ANON_NAMES):
        return "owner", owner_name

    for c in palace.get("participants", []):
        if name_raw.lower() in (c.get("name", "").lower(), c["id"].lower()):
            return c["id"], c.get("name", c["id"])

    return "unknown", name_raw


def _extract_messages(jsonl_path: Path, session_key: str,
                      gw_id: str, gw_name: str, palace: dict,
                      last_lines: int) -> tuple[list[dict], int]:
    """Read new lines from a session jsonl. Return (messages, total_lines)."""
    is_group = ":group:" in session_key
    group_id = session_key.split(":group:")[-1] if is_group else ""

    messages = []
    total = 0

    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, 1):
                total = lineno
                if lineno <= last_lines:
                    continue
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "message":
                    continue

                msg = entry.get("message", {})
                role = msg.get("role", "")
                ts = entry.get("timestamp", "")
                content = msg.get("content", [])
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]

                if role == "user":
                    text_parts = [
                        c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") == "text"
                    ]
                    parsed = _parse_user_message("\n".join(text_parts))
                    if not parsed:
                        continue

                    sid, sdisp = _resolve_sender(parsed["sender_info"], palace)
                    ts_sec = ts[:19]  # second precision to avoid over-merging same-content messages
                    ch = _content_hash(parsed["text"])

                    if is_group:
                        mid = f"group.{group_id}.{sid}.{ts_sec}.{ch}"
                        messages.append({
                            "id": mid, "type": "group", "route": group_id,
                            "sender": sid, "sender_display": sdisp,
                            "target": "", "timestamp": ts,
                            "content": parsed["text"],
                            "verified": "truth", "source_gw": gw_id,
                        })
                    else:
                        mid = f"chat.{sid}>{gw_id}.{ts_sec}.{ch}"
                        messages.append({
                            "id": mid, "type": "chat",
                            "route": f"{sid}>{gw_id}",
                            "sender": sid, "sender_display": sdisp,
                            "target": gw_id, "timestamp": ts,
                            "content": parsed["text"],
                            "verified": "truth", "source_gw": gw_id,
                        })

                elif role == "assistant":
                    text = _parse_assistant_text(content)
                    if not text or text in ("NO_REPLY", "HEARTBEAT_OK"):
                        continue

                    ts_sec = ts[:19]
                    ch = _content_hash(text)

                    if is_group:
                        mid = f"group.{group_id}.{gw_id}.{ts_sec}.{ch}"
                        messages.append({
                            "id": mid, "type": "group", "route": group_id,
                            "sender": gw_id, "sender_display": gw_name,
                            "target": "", "timestamp": ts,
                            "content": text,
                            "verified": "truth", "source_gw": gw_id,
                        })
                    else:
                        mid = f"chat.{gw_id}>owner.{ts_sec}.{ch}"
                        messages.append({
                            "id": mid, "type": "chat",
                            "route": f"{gw_id}>owner",
                            "sender": gw_id, "sender_display": gw_name,
                            "target": "owner", "timestamp": ts,
                            "content": text,
                            "verified": "truth", "source_gw": gw_id,
                        })
    except Exception as e:
        print(f"[eunuch] read error {jsonl_path.name}: {e}")

    return messages, total


# ── Collector ─────────────────────────────────────────────────

_read_progress: dict[str, int] = {}  # jsonl path → line count


_session_key_cache: dict[str, str] = {}  # jsonl path -> detected session key


def reset_read_progress():
    """Reset collector read progress so all session messages get re-collected."""
    global _read_progress, _session_key_cache
    _read_progress.clear()
    _session_key_cache.clear()
    print("[eunuch] 采集进度已重置")


def _detect_session_key(jsonl_path: Path) -> str:
    """Peek into a jsonl file to determine its session type.
    Returns a synthetic session key like 'agent:main:telegram:group:-100xxx'
    or 'agent:main:telegram:dm:xxx' or 'agent:main:main'."""
    cached = _session_key_cache.get(str(jsonl_path))
    if cached:
        return cached

    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
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
                if msg.get("role") != "user":
                    continue
                content = msg.get("content", [])
                if isinstance(content, str):
                    text = content
                else:
                    text = " ".join(
                        c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") == "text"
                    )
                # Look for group conversation_label
                m = re.search(r'"conversation_label":\s*"[^"]*?(-\d{10,})', text)
                if m:
                    gid = m.group(1)
                    key = f"agent:main:telegram:group:{gid}"
                    _session_key_cache[str(jsonl_path)] = key
                    return key
                # If no group label found, check for sender info (DM)
                if "Sender" in text and "untrusted metadata" in text:
                    key = "agent:main:telegram:dm:unknown"
                    _session_key_cache[str(jsonl_path)] = key
                    return key
                break
    except Exception:
        pass

    key = "agent:main:main"
    _session_key_cache[str(jsonl_path)] = key
    return key


def _get_active_session_files(sessions_dir: Path) -> list[Path]:
    """Return session files to scan.

    Always includes ALL .jsonl files (not just those listed in sessions.json)
    so that group session files are never missed — even if only the DM session
    is listed as "active".  The per-file _read_progress tracking makes this
    cheap: already-read lines are skipped.
    """
    return list(sessions_dir.glob("*.jsonl"))


def collect_once(pool: MessagePool):
    palace = _load_palace()
    gateways = _load_gateways()
    if not gateways:
        return

    for gw in gateways:
        state_dir = HUB_DIR / gw.get("state_dir", "")
        sessions_dir = state_dir / "agents" / "main" / "sessions"
        if not sessions_dir.exists():
            continue

        added_total = 0
        for jsonl_file in _get_active_session_files(sessions_dir):
            fk = str(jsonl_file)
            last = _read_progress.get(fk, 0)

            skey = _detect_session_key(jsonl_file)

            msgs, new_total = _extract_messages(
                jsonl_file, skey, gw["id"], gw.get("name", gw["id"]),
                palace, last)
            _read_progress[fk] = new_total

            for m in msgs:
                if pool.add(m):
                    added_total += 1

        if added_total:
            log_action(pool, "\u6536\u96c6", f"\u4ece {gw['name']} \u6536\u96c6\u4e86 {added_total} \u6761\u65b0\u6d88\u606f", gw["id"])

    pool.persist()


# ── Query API ─────────────────────────────────────────────────

_pool: MessagePool | None = None


def get_pool() -> MessagePool:
    global _pool
    if _pool is None:
        _pool = MessagePool(POOL_FILE)
    return _pool


def query_ext_message(agent_id: str, since: str = None,
                      limit: int = 50) -> dict:
    """Return group + whisper/decree targeted at agent.
    Chat messages excluded (agent has those in its own session)."""
    pool = get_pool()

    group_msgs = pool.query(types=["group"], since=since, limit=limit)
    targeted = pool.query(
        types=["whisper", "decree"], target=agent_id,
        since=since, limit=limit)

    merged = {m["id"]: m for m in group_msgs + targeted}
    ordered = sorted(merged.values(), key=lambda m: m.get("timestamp", ""))
    if len(ordered) > limit:
        ordered = ordered[-limit:]

    log_action(pool, "查询", f"{agent_id} 查询消息池，返回 {len(ordered)} 条", agent_id)
    result = _format_briefing(ordered, agent_id)
    if ordered:
        result["latest_timestamp"] = ordered[-1]["timestamp"]

    return result


def _format_briefing(messages: list[dict], agent_id: str) -> dict:
    palace = _load_palace()
    sections = {
        "群组讨论": [],
        "匿名消息": [],
        "系统通知": [],
    }

    for msg in messages:
        ts = msg["timestamp"][:16].replace("T", " ")
        sender = msg["sender_display"]
        content = msg["content"]
        if len(content) > 300:
            content = content[:300] + "..."

        if msg["type"] == "group":
            sections["群组讨论"].append(f"[{ts}] {sender}: {content}")
        elif msg["type"] == "whisper":
            # Anonymous message — no sender attribution
            sections["匿名消息"].append(f"[{ts}] {content}")
        elif msg["type"] == "decree":
            sections["系统通知"].append(f"[{ts}] {sender}: {content}")

    lines = ["# 消息池摘要", ""]
    for title, items in sections.items():
        lines.append(f"## {title}")
        if items:
            lines.extend(items)
        else:
            lines.append("（暂无）")
        lines.append("")

    return {
        "text": "\n".join(lines),
        "message_count": len(messages),
        "sections": {k: len(v) for k, v in sections.items()},
    }


# ── Whisper anonymity enforcement ─────────────────────────────

_SELF_REF_PATTERNS = [
    "妾身", "臣妾", "本宫", "奴家", "小女子",
    "妹妹我", "姐姐我", "我是",
]


def _check_whisper_anonymity(content: str, agent_id: str, palace: dict) -> str | None:
    """Return error string if whisper content leaks sender identity, else None."""
    # Collect sender's name and aliases
    forbidden = set()
    for c in palace.get("participants", []):
        if c["id"] == agent_id:
            forbidden.add(c.get("name", ""))
            for a in c.get("aliases", []):
                forbidden.add(a)
            break
    forbidden.add(agent_id)
    forbidden.discard("")

    cl = content.lower()
    # Check sender name / aliases
    for word in forbidden:
        if word.lower() in cl:
            return (f"密语被消息池驳回：内容中出现了你的名字「{word}」，"
                    "会暴露身份。whisper 必须匿名，请用第三人称重写，"
                    "不要自称、不要提及自己的名字或身份线索。")

    # Check self-referential pronouns
    for pat in _SELF_REF_PATTERNS:
        if pat in content:
            return (f"密语被消息池驳回：内容中出现了自称「{pat}」，"
                    "会暴露身份。whisper 是匿名风言风语，对方不知道是谁说的。"
                    "请用「听说」「据传」「有人看见」等第三人称口吻重写。")

    return None


# ── Submit API ────────────────────────────────────────────────

def submit_actions(agent_id: str, actions: list[dict]) -> dict:
    palace = _load_palace()
    gateways = _load_gateways()
    proxy = palace.get("proxy", "")
    owner_cfg = palace.get("owner", {})

    id_to_token = {}
    id_to_name = {}
    name_to_id = {}
    for c in palace.get("participants", []):
        id_to_token[c["id"]] = c.get("botToken", "")
        id_to_name[c["id"]] = c.get("name", c["id"])
        name_to_id[c.get("name", "").lower()] = c["id"]
        name_to_id[c["id"].lower()] = c["id"]

    token = id_to_token.get(agent_id, "")
    sender_name = id_to_name.get(agent_id, agent_id)
    pool = get_pool()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    results = []
    for i, act in enumerate(actions):
        atype = act.get("type", "")
        ato = act.get("to", "")
        acontent = act.get("content", "").strip()
        if not acontent:
            results.append({"status": "skip", "reason": "empty"})
            continue

        ts_tag = f"{now[:-1]}{i:02d}Z"  # unique per action
        ch = _content_hash(acontent)

        if atype == "group":
            gid = ato or palace.get("telegram", {}).get("groupId", "")
            if not token or not gid:
                results.append({"status": "error", "error": "no token/group"})
                continue
            # Always add to pool first (guaranteed)
            pool.add({
                "id": f"group.{gid}.{agent_id}.{ts_tag}.{ch}",
                "type": "group", "route": gid,
                "sender": agent_id, "sender_display": sender_name,
                "target": "", "timestamp": ts_tag,
                "content": acontent,
                "verified": "truth", "source_gw": agent_id,
            })
            log_action(pool, "群发言", f"代 {sender_name} 在群内发言", agent_id)
            # Then attempt Telegram delivery (best-effort)
            r = _tg_api(token, "sendMessage",
                        {"chat_id": gid, "text": acontent}, proxy)
            if r.get("ok"):
                results.append({"status": "sent", "type": "group"})
            else:
                print(f"[eunuch] TG群发失败({agent_id}): {r.get('description', '?')}")
                results.append({"status": "sent_pool_only", "type": "group", "tg_error": r.get("description")})

        elif atype == "dm":
            dm_target = ato or "owner"
            # Resolve target: "owner" or another agent id/name
            is_owner = dm_target.lower() in ("owner", "用户", "主人")
            if not is_owner:
                dm_target = name_to_id.get(dm_target.lower(), dm_target.lower())

            # Always add to pool
            target_label = "owner" if is_owner else dm_target
            pool.add({
                "id": f"chat.{agent_id}>{target_label}.{ts_tag}.{ch}",
                "type": "chat", "route": f"{agent_id}>{target_label}",
                "sender": agent_id, "sender_display": sender_name,
                "target": target_label, "timestamp": ts_tag,
                "content": acontent,
                "verified": "truth", "source_gw": agent_id,
            })

            if is_owner:
                # Agent → Owner: send via Telegram
                owner_cid = owner_cfg.get("telegramUserId", "")
                if not token or not owner_cid:
                    log_action(pool, "私信投递", f"代 {sender_name} 向用户递了私信（TG配置缺失）", agent_id)
                    results.append({"status": "sent_pool_only", "type": "dm", "error": "no token/owner"})
                    continue
                log_action(pool, "私信投递", f"代 {sender_name} 向用户递了私信", agent_id)
                r = _tg_api(token, "sendMessage", {
                    "chat_id": owner_cid,
                    "text": f"\U0001f48c {sender_name}私聊你说:\n\n{acontent}",
                }, proxy)
                if r.get("ok"):
                    results.append({"status": "sent", "type": "dm", "to": "owner"})
                else:
                    print(f"[eunuch] TG私信失败({agent_id}→owner): {r.get('description', '?')}")
                    results.append({"status": "sent_pool_only", "type": "dm", "to": "owner", "tg_error": r.get("description")})
            else:
                # Agent → Agent: pool only, no Telegram
                log_action(pool, "私信投递", f"代 {sender_name} 向 {dm_target} 递了私信", agent_id)
                results.append({"status": "stored", "type": "dm", "to": dm_target})

        elif atype == "whisper":
            tid = name_to_id.get(ato.lower(), ato.lower())
            if not tid or tid == agent_id:
                results.append({"status": "error", "error": f"bad target: {ato}"})
                continue
            # Enforce anonymity: reject content that leaks sender identity
            anon_err = _check_whisper_anonymity(acontent, agent_id, palace)
            if anon_err:
                print(f"[eunuch] whisper rejected ({agent_id}→{tid}): {anon_err[:60]}")
                results.append({"status": "rejected", "type": "whisper", "error": anon_err})
                continue
            # Whisper = anonymous gossip: target sees it but doesn't know who sent it
            # sender is kept for admin tracking; sender_display is anonymous
            pool.add({
                "id": f"whisper.anon>{tid}.{ts_tag}.{ch}",
                "type": "whisper", "route": f"anon>{tid}",
                "sender": agent_id, "sender_display": "风言风语",
                "target": tid, "timestamp": ts_tag,
                "content": acontent,
                "verified": "unverified", "source_gw": agent_id,
            })
            results.append({"status": "stored", "type": "whisper", "to": tid})
            log_action(pool, "风言散布", f"有人向 {tid} 散布了风言风语", agent_id)

        else:
            results.append({"status": "error", "error": f"unknown type: {atype}"})

    pool.persist()
    return {"results": results}


def ledger_all(limit: int = 5000) -> dict:
    """Return ALL pool entries (including eunuch_log) for the admin ledger view."""
    pool = get_pool()
    entries = pool.get_all(limit)
    entries.sort(key=lambda m: m.get("timestamp", ""))
    counts = pool.count_by_type()
    return {
        "entries": entries,
        "total": pool.size,
        "counts": counts,
    }


# ── Background Thread ─────────────────────────────────────────

_thread = None
_stop = threading.Event()


def start_eunuch():
    global _thread, _pool
    if _thread and _thread.is_alive():
        return

    _pool = MessagePool(POOL_FILE)
    palace = _load_palace()
    interval = palace.get("sync", {}).get("intervalSeconds", 15)
    _stop.clear()

    _compact_counter = [0]

    def _loop():
        while not _stop.is_set():
            try:
                collect_once(_pool)
            except Exception as e:
                print(f"[eunuch] collect error: {e}")

            # Check for oversized sessions every ~5 minutes (20 * 15s)
            _compact_counter[0] += 1
            if _compact_counter[0] >= 20:
                _compact_counter[0] = 0
                try:
                    from compact_session import check_and_auto_compact
                    gateways = _load_gateways()
                    for gw in gateways:
                        compacted = check_and_auto_compact(gw["id"])
                        if compacted:
                            log_action(_pool, "记忆压缩",
                                       f"{gw.get('name', gw['id'])} 自动压缩 {len(compacted)} 个会话",
                                       gw["id"])
                except Exception as e:
                    print(f"[eunuch] auto-compact error: {e}")

            _stop.wait(interval)

    _thread = threading.Thread(target=_loop, daemon=True, name="eunuch")
    _thread.start()
    print(f"[eunuch] 消息池上线 (collect={interval}s, pool={_pool.size} msgs)")


def stop_eunuch():
    _stop.set()
    if _thread:
        _thread.join(timeout=5)
    if _pool:
        _pool.persist()
    print("[eunuch] 消息池下线")
