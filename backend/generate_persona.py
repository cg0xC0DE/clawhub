"""
generate_persona.py — 自动生成穿越风格专家人格文件

通过 LLM 根据角色的历史身份，生成 SOUL.md / IDENTITY.md / AGENTS.md。
在 gateway 创建时调用（后台线程）。

LLM 会自动判断角色是已故还是在世：
- 已故角色使用"死穿"叙事（死亡记忆 → 黑暗 → 醒来在讨论场）
- 在世角色使用"生穿"叙事（重大事件 → 眼一闭再一睁 → 醒来在讨论场）
"""

import json
import re
import urllib.request
import urllib.error
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
GATEWAYS_DIR = BACKEND_DIR.parent / "gateways"


# ── Model registry ────────────────────────────────────────
MODEL_REGISTRY = {
    "gpt-5.4":            {"provider": "openai",       "model_id": "gpt-5.4",                   "label": "GPT-5.4"},
    "gpt-5.4-pro":        {"provider": "openai",       "model_id": "gpt-5.4-pro",               "label": "GPT-5.4 Pro"},
    "claude-opus-4.6":    {"provider": "anthropic",    "model_id": "claude-opus-4-6",           "label": "Claude Opus 4.6"},
    "claude-sonnet-4.6":  {"provider": "anthropic",    "model_id": "claude-sonnet-4-6",         "label": "Claude Sonnet 4.6"},
    "gemini-3.1-pro":     {"provider": "google",       "model_id": "gemini-3.1-pro-preview",    "label": "Gemini 3.1 Pro"},
    "kimi-k2.5":          {"provider": "kimi-coding",  "model_id": "k2p5",                      "label": "Kimi K2.5"},
    "minimax-m2.5":       {"provider": "minimax",      "model_id": "MiniMax-M2.5",              "label": "MiniMax M2.5"},
}

DEFAULT_MODEL = "gpt-5.4"

_PROVIDER_URLS = {
    "anthropic":   "https://api.anthropic.com/v1/messages",
    "openai":      "https://api.openai.com/v1/chat/completions",
    "google":      "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    "kimi-coding": "https://api.kimi.com/coding/v1/messages",
    "minimax":     "https://api.minimax.io/v1/text/chatcompletion_v2",
}

PROXY = "http://127.0.0.1:10020"


# ── Key finder ────────────────────────────────────────────
def _get_api_key(provider: str) -> str:
    """Find an API key for a specific provider from any gateway's auth-profiles.json."""
    for gw_dir in GATEWAYS_DIR.iterdir():
        auth_path = gw_dir / "state" / "agents" / "main" / "agent" / "auth-profiles.json"
        if not auth_path.exists():
            continue
        try:
            data = json.loads(auth_path.read_text(encoding="utf-8"))
            for pid, prof in data.get("profiles", {}).items():
                if prof.get("provider") == provider and prof.get("key"):
                    return prof["key"]
        except Exception:
            continue
    return ""


def get_available_models() -> list[dict]:
    """Return list of models that have API keys available."""
    available = []
    checked_providers = {}
    for model_key, info in MODEL_REGISTRY.items():
        prov = info["provider"]
        if prov not in checked_providers:
            checked_providers[prov] = bool(_get_api_key(prov))
        if checked_providers[prov]:
            available.append({"key": model_key, "label": info["label"], "provider": prov})
    return available


# ── LLM call functions ────────────────────────────────────
def _make_opener(use_proxy: bool = True):
    if use_proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
        )
    return urllib.request.build_opener()


def _call_anthropic(api_key: str, model_id: str, prompt: str,
                    max_tokens: int = 16384, base_url: str = "") -> str:
    url = base_url or _PROVIDER_URLS["anthropic"]
    payload = {
        "model": model_id,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with _make_opener().open(req, timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Anthropic API error {e.code} from {url}: {body}") from e
    for block in result.get("content", []):
        if block.get("type") == "text":
            return block["text"].strip()
    return ""


def _call_openai_compatible(api_key: str, model_id: str, prompt: str,
                            base_url: str, max_tokens: int = 16384) -> str:
    # Newer OpenAI models (e.g. gpt-5.4) require max_completion_tokens
    token_key = "max_completion_tokens" if "openai.com" in base_url else "max_tokens"
    payload = {
        "model": model_id,
        token_key: max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        base_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with _make_opener().open(req, timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"OpenAI-compatible API error {e.code} from {base_url}: {body}") from e
    choices = result.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        # Some reasoning models (e.g. MiniMax-M2.5) put output in reasoning_content
        text = msg.get("content", "") or msg.get("reasoning_content", "")
        return text.strip() if text else ""
    return ""


def _call_google(api_key: str, model_id: str, prompt: str) -> str:
    url = _PROVIDER_URLS["google"].format(model=model_id) + f"?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 16384},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with _make_opener().open(req, timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Google API error {e.code}: {body}") from e
    candidates = result.get("candidates", [])
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        if parts:
            return parts[0].get("text", "").strip()
    return ""


def _call_llm(prompt: str, model: str = DEFAULT_MODEL) -> str:
    """Unified LLM call. Raises on failure."""
    info = MODEL_REGISTRY.get(model)
    if not info:
        raise ValueError(f"Unknown model: {model}. Available: {list(MODEL_REGISTRY.keys())}")

    provider = info["provider"]
    model_id = info["model_id"]
    api_key = _get_api_key(provider)
    if not api_key:
        raise RuntimeError(f"No API key found for provider '{provider}'")

    print(f"[persona_gen] Using model={model} (provider={provider}, model_id={model_id})")

    if provider == "anthropic":
        return _call_anthropic(api_key, model_id, prompt)
    elif provider == "kimi-coding":
        # Kimi Coding uses Anthropic protocol at api.kimi.com
        return _call_anthropic(api_key, model_id, prompt,
                               base_url=_PROVIDER_URLS["kimi-coding"])
    elif provider == "google":
        return _call_google(api_key, model_id, prompt)
    else:
        base_url = _PROVIDER_URLS[provider]
        return _call_openai_compatible(api_key, model_id, prompt, base_url)


# ── Prompt builder (loaded from private file, fallback to example) ──
def _build_prompt(agent_id: str, character_name: str) -> str:
    try:
        from persona_prompts import build_prompt
    except ImportError:
        from persona_prompts_example import build_prompt
    return build_prompt(agent_id, character_name)


def _parse_three_files(text: str) -> dict[str, str]:
    """Parse LLM output into three file contents."""
    result = {}
    markers = [
        ("SOUL.MD", "SOUL.md"),
        ("IDENTITY.MD", "IDENTITY.md"),
        ("AGENTS.MD", "AGENTS.md"),
    ]

    for i, (marker, filename) in enumerate(markers):
        pattern = f"==={marker}==="
        start = text.find(pattern)
        if start == -1:
            continue
        start += len(pattern)

        # Find end: next marker or end of text
        end = len(text)
        for j in range(i + 1, len(markers)):
            next_start = text.find(f"==={markers[j][0]}===", start)
            if next_start != -1:
                end = next_start
                break

        content = text[start:end].strip()
        if content:
            result[filename] = content

    return result


def generate_persona_standalone(character_name: str,
                                model: str = DEFAULT_MODEL) -> dict[str, str]:
    """
    Generate persona files via LLM without writing to any gateway.
    Returns dict of {filename: content} for saving to persona library.
    """
    agent_id = "expert"  # placeholder for prompt template
    prompt = _build_prompt(agent_id, character_name)
    print(f"[persona_gen] Standalone generation for {character_name} with model={model}...")

    text = _call_llm(prompt, model=model)
    files = _parse_three_files(text)

    if len(files) < 3:
        print(f"[persona_gen] WARNING: only parsed {len(files)}/3 files from LLM response")

    for filename, content in files.items():
        print(f"[persona_gen]   {filename}: {len(content)} chars")

    return files


def generate_persona(agent_id: str, character_name: str,
                     model: str = DEFAULT_MODEL, force: bool = False) -> dict[str, str]:
    """
    Generate transmigration persona files via LLM.
    LLM auto-detects deceased vs alive characters and uses
    death-transmigration or life-transmigration narrative accordingly.
    Returns dict of {filename: content} (also saved to disk).
    """
    ws_dir = GATEWAYS_DIR / agent_id / "state" / "workspace"
    if not ws_dir.exists():
        ws_dir.mkdir(parents=True, exist_ok=True)

    # Skip if SOUL.md already has transmigration content (unless forced)
    if not force:
        soul_path = ws_dir / "SOUL.md"
        if soul_path.exists():
            existing = soul_path.read_text(encoding="utf-8")
            if "前世之魂" in existing or "你是谁" in existing:
                print(f"[persona_gen] {agent_id} already has transmigration SOUL.md, skipping")
                return {}

    prompt = _build_prompt(agent_id, character_name)
    print(f"[persona_gen] Generating persona for {character_name} ({agent_id}) with model={model}...")

    try:
        text = _call_llm(prompt, model=model)
        files = _parse_three_files(text)

        if len(files) < 3:
            print(f"[persona_gen] WARNING: only parsed {len(files)}/3 files from LLM response")

        for filename, content in files.items():
            filepath = ws_dir / filename
            # Back up existing file
            if filepath.exists():
                bak = ws_dir / f"{filename}.bak.prev"
                filepath.replace(bak)
            filepath.write_text(content, encoding="utf-8")
            print(f"[persona_gen]   {filename}: {len(content)} chars")

        return files

    except Exception as e:
        print(f"[persona_gen] LLM generation failed for {character_name}: {e}")
        import traceback
        traceback.print_exc()
        raise
