"""
proxy_config.py — Centralized proxy configuration.

Reads the "proxy" field from palace.json as the single source of truth.
All modules should import from here instead of hardcoding proxy addresses.

If palace.json has no "proxy" field or it is empty, no proxy is used.
"""

import json
from pathlib import Path
from urllib.parse import urlparse

HUB_DIR = Path(__file__).resolve().parent.parent


def get_proxy() -> str:
    """Return the HTTP proxy URL from palace.json, or empty string if not set."""
    palace_path = HUB_DIR / "palace.json"
    try:
        data = json.loads(palace_path.read_text(encoding="utf-8"))
        return (data.get("proxy") or "").strip()
    except Exception:
        return ""


def get_socks_proxy() -> str:
    """Derive a SOCKS5 proxy URL from the HTTP proxy (same host:port).
    Returns empty string if no proxy configured."""
    http_proxy = get_proxy()
    if not http_proxy:
        return ""
    parsed = urlparse(http_proxy)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 1080
    return f"socks5://{host}:{port}"


def get_proxy_env(env: dict = None) -> dict:
    """Return a copy of env (or os.environ) with proxy vars set.
    If no proxy configured, proxy vars are removed to avoid interference."""
    import os
    env = (env or os.environ).copy()
    http_proxy = get_proxy()
    socks_proxy = get_socks_proxy()
    if http_proxy:
        env["HTTP_PROXY"] = http_proxy
        env["HTTPS_PROXY"] = http_proxy
        env["ALL_PROXY"] = socks_proxy
    else:
        # Ensure no stale proxy env vars leak through
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                     "http_proxy", "https_proxy", "all_proxy"):
            env.pop(key, None)
    return env


def make_urllib_opener(proxy: str = None):
    """Create a urllib opener with proxy support.
    If proxy is None, reads from palace.json.
    If proxy is empty string, no proxy is used."""
    import urllib.request
    if proxy is None:
        proxy = get_proxy()
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    return urllib.request.build_opener()
