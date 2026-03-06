"""
compact_session.py — 会话压缩与记忆归档

当 session jsonl 超过阈值时：
1. 读取对话历史，调用 LLM 生成子弹列表摘要
2. 追加到 workspace/MEMORY.md（中期记忆）
3. MEMORY.md 超过 50 条时，FIFO 归档到 memory/archive/YYYY-MM.md
4. 保留 jsonl 最后 10 行，重命名旧文件为 .reset，新建继续
"""

import json
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

HUB_DIR = Path(__file__).resolve().parent.parent

COMPACT_THRESHOLD_BYTES = 100 * 1024  # 100 KB
MEMORY_MAX_ENTRIES = 50
CARRY_OVER_LINES = 10  # 保留最后 N 行到新 session


# ── LLM 摘要调用 ──────────────────────────────────────────────

def _call_anthropic(api_key: str, user_prompt: str, proxy: str = None) -> str:
    """调用 Anthropic Messages API 生成摘要（使用最便宜的 haiku 模型）。"""
    url = "https://api.anthropic.com/v1/messages"
    payload = {
        "model": "claude-haiku-4-5",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
    )
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    else:
        opener = urllib.request.build_opener()

    with opener.open(req, timeout=60) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    content = result.get("content", [])
    for block in content:
        if block.get("type") == "text":
            return block["text"].strip()
    return ""


def _get_summary_key(gw_dir: Path) -> tuple[str, str]:
    """
    从 auth-profiles.json 读取可用于摘要的 API key。
    优先 anthropic（标准 API），返回 (provider, key)。
    """
    auth_path = gw_dir / "state" / "agents" / "main" / "agent" / "auth-profiles.json"
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
        profiles = data.get("profiles", {})
        for profile_id, profile in profiles.items():
            if profile.get("provider") == "anthropic":
                return ("anthropic", profile.get("key", ""))
    except Exception:
        pass
    return ("", "")


# ── 对话历史提取 ───────────────────────────────────────────────

def _extract_dialogue(jsonl_path: Path) -> list[dict]:
    """从 jsonl 提取 user/assistant 对话对，返回 [{role, text}]。"""
    dialogue = []
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
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
                role = msg.get("role", "")
                if role not in ("user", "assistant"):
                    continue
                content = msg.get("content", "")
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block["text"])
                    text = "\n".join(parts)
                else:
                    text = str(content)
                # Skip heartbeat system messages and very short entries
                if len(text.strip()) < 10:
                    continue
                # Skip pure HEARTBEAT_OK lines
                if text.strip() == "HEARTBEAT_OK":
                    continue
                dialogue.append({"role": role, "text": text[:2000]})
    except Exception:
        pass
    return dialogue


def _build_summary_prompt(dialogue: list[dict], agent_name: str) -> list[dict]:
    """构建摘要 prompt。"""
    convo_text = ""
    for turn in dialogue[-60:]:  # 最多取最近 60 轮
        role_label = "用户" if turn["role"] == "user" else agent_name
        convo_text += f"[{role_label}]: {turn['text'][:500]}\n\n"

    system = (
        "你是一个记忆整理助手。请将以下对话历史整理成简洁的子弹列表记忆条目。\n"
        "每条记忆格式：- YYYY-MM-DD · [地点/场景] · [人物] · [事件] · [起因] · [结果] · [情绪/影响]\n"
        "要求：\n"
        "- 只记录有实质内容的事件，忽略闲聊和重复的心跳检查\n"
        "- 每条控制在 100 字以内\n"
        "- 最多生成 10 条，按时间排序\n"
        "- 如果对话内容不值得记录，返回空字符串\n"
        "直接输出子弹列表，不要任何前言或解释。"
    )

    return [
        {"role": "user", "content": f"{system}\n\n对话历史：\n{convo_text}"}
    ]


# ── MEMORY.md FIFO 管理 ────────────────────────────────────────

def _parse_memory_entries(content: str) -> tuple[str, list[str]]:
    """解析 MEMORY.md，返回 (header_without_entries_section, [bullet_entries])。
    header 是 '## 记忆条目' 之前的所有内容。"""
    SECTION_MARKER = "## 记忆条目"
    if SECTION_MARKER in content:
        idx = content.index(SECTION_MARKER)
        header = content[:idx].rstrip()
        entries_block = content[idx + len(SECTION_MARKER):]
        bullet_entries = [
            l for l in entries_block.split("\n") if l.startswith("- ")
        ]
    else:
        header = content.rstrip()
        bullet_entries = []
    return header, bullet_entries


def _archive_old_entries(archive_dir: Path, entries: list[str]) -> None:
    """将旧条目归档到 memory/archive/YYYY-MM.md。"""
    archive_dir.mkdir(parents=True, exist_ok=True)
    month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    archive_file = archive_dir / f"{month_key}.md"

    existing = ""
    if archive_file.exists():
        existing = archive_file.read_text(encoding="utf-8")

    new_content = existing.rstrip()
    if new_content:
        new_content += "\n"
    new_content += "\n".join(entries) + "\n"
    archive_file.write_text(new_content, encoding="utf-8")


def _update_memory_md(memory_path: Path, new_bullets: str) -> int:
    """
    追加新子弹条目到 MEMORY.md，超过 50 条时 FIFO 归档。
    返回当前条目总数。
    """
    if not new_bullets.strip():
        return 0

    existing = ""
    if memory_path.exists():
        existing = memory_path.read_text(encoding="utf-8")

    header, entries = _parse_memory_entries(existing)

    # 解析新条目
    new_lines = [l for l in new_bullets.split("\n") if l.startswith("- ")]
    entries.extend(new_lines)

    # FIFO 归档超出部分
    archive_dir = memory_path.parent / "memory" / "archive"
    if len(entries) > MEMORY_MAX_ENTRIES:
        overflow = entries[: len(entries) - MEMORY_MAX_ENTRIES]
        entries = entries[len(entries) - MEMORY_MAX_ENTRIES:]
        _archive_old_entries(archive_dir, overflow)

    # 重写 MEMORY.md
    content = header + "\n\n## 记忆条目\n\n" + "\n".join(entries) + "\n"
    memory_path.write_text(content, encoding="utf-8")
    return len(entries)


# ── Session Reset ──────────────────────────────────────────────

def _reset_session(jsonl_path: Path) -> Path:
    """
    保留最后 CARRY_OVER_LINES 行，重命名旧文件为 .reset，
    将保留行写入同名新文件（openclaw 会接管并继续追加）。
    返回新文件路径（与原路径相同）。
    """
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
    except Exception:
        return jsonl_path

    # 重命名旧文件
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S.") + "000Z"
    reset_path = jsonl_path.with_suffix(f".jsonl.reset.{ts}")
    jsonl_path.rename(reset_path)

    # 保留最后 N 行写入新文件
    carry = all_lines[-CARRY_OVER_LINES:] if len(all_lines) > CARRY_OVER_LINES else all_lines
    # 确保第一行是 session header（从旧文件找）
    session_header = None
    for line in all_lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get("type") == "session":
                session_header = line + "\n"
                break
        except Exception:
            continue

    with open(jsonl_path, "w", encoding="utf-8") as f:
        if session_header:
            f.write(session_header)
        for line in carry:
            # Don't duplicate session header
            try:
                entry = json.loads(line.strip())
                if entry.get("type") == "session":
                    continue
            except Exception:
                pass
            f.write(line)

    return jsonl_path


# ── 主入口 ────────────────────────────────────────────────────

def compact_gateway_sessions(gw_id: str, force: bool = False) -> dict:
    """
    检查并压缩指定 gateway 的所有活跃 session jsonl。
    force=True 时跳过大小检查。
    返回操作摘要。
    """
    from gateway_manager import HUB_DIR as _HUB, GATEWAYS_DIR
    gw_dir = GATEWAYS_DIR / gw_id
    if not gw_dir.exists():
        return {"error": f"Gateway '{gw_id}' not found"}

    sessions_dir = gw_dir / "state" / "agents" / "main" / "sessions"
    workspace_dir = gw_dir / "state" / "workspace"
    memory_path = workspace_dir / "MEMORY.md"

    if not sessions_dir.exists():
        return {"error": "Sessions directory not found"}

    llm_provider, api_key = _get_summary_key(gw_dir)
    proxy = "http://127.0.0.1:10020"

    results = []

    for jsonl_file in sessions_dir.glob("*.jsonl"):
        size = jsonl_file.stat().st_size
        if not force and size < COMPACT_THRESHOLD_BYTES:
            results.append({
                "file": jsonl_file.name,
                "size_kb": round(size / 1024, 1),
                "action": "skipped",
                "reason": f"below threshold ({COMPACT_THRESHOLD_BYTES // 1024}KB)",
            })
            continue

        # Extract dialogue
        dialogue = _extract_dialogue(jsonl_file)
        if len(dialogue) < 4:
            results.append({
                "file": jsonl_file.name,
                "size_kb": round(size / 1024, 1),
                "action": "skipped",
                "reason": "too few dialogue turns",
            })
            continue

        # Generate summary via LLM
        summary_bullets = ""
        if api_key:
            try:
                prompt_msgs = _build_summary_prompt(dialogue, gw_id)
                user_prompt = prompt_msgs[0]["content"]
                summary_bullets = _call_anthropic(api_key, user_prompt, proxy=proxy)
            except Exception as e:
                summary_bullets = ""
                results.append({
                    "file": jsonl_file.name,
                    "size_kb": round(size / 1024, 1),
                    "action": "warn",
                    "reason": f"LLM summary failed: {e}",
                })

        # Update MEMORY.md
        entry_count = 0
        if summary_bullets:
            try:
                entry_count = _update_memory_md(memory_path, summary_bullets)
            except Exception as e:
                results.append({
                    "file": jsonl_file.name,
                    "action": "warn",
                    "reason": f"MEMORY.md update failed: {e}",
                })

        # Reset session
        try:
            _reset_session(jsonl_file)
        except Exception as e:
            results.append({
                "file": jsonl_file.name,
                "action": "error",
                "reason": f"session reset failed: {e}",
            })
            continue

        results.append({
            "file": jsonl_file.name,
            "size_kb": round(size / 1024, 1),
            "action": "compacted",
            "dialogue_turns": len(dialogue),
            "memory_entries": entry_count,
            "summary_generated": bool(summary_bullets),
        })

    return {"gw_id": gw_id, "results": results}


def check_and_auto_compact(gw_id: str) -> list[dict]:
    """
    静默检查并自动压缩超阈值的 session。
    供 eunuch 后台线程调用。返回被压缩的文件列表。
    """
    from gateway_manager import GATEWAYS_DIR
    gw_dir = GATEWAYS_DIR / gw_id
    sessions_dir = gw_dir / "state" / "agents" / "main" / "sessions"
    if not sessions_dir.exists():
        return []

    compacted = []
    for jsonl_file in sessions_dir.glob("*.jsonl"):
        if jsonl_file.stat().st_size >= COMPACT_THRESHOLD_BYTES:
            compacted.append(jsonl_file.name)

    if compacted:
        compact_gateway_sessions(gw_id, force=False)

    return compacted
