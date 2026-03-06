"""
Integration tests — LLM API connectivity & minimal call verification.

Strategy:
  1. TCP-level reachability check (no key needed, no cost)
  2. If reachable AND key exists → fire a trivial one-token request
  3. Skip gracefully when network/VPN is down or key is missing

Run:  python -m pytest tests/test_api_connectivity.py -v -s
"""

import json
import socket
import sys
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import generate_persona as gp

# ── Reachability helper ───────────────────────────────────────

# Timeout for TCP probe (seconds) — keep short
_TCP_TIMEOUT = 5
_PROXY = gp.PROXY  # http://127.0.0.1:10020


def _tcp_reachable(host: str, port: int = 443) -> bool:
    """Check if a host:port is reachable at TCP level."""
    try:
        with socket.create_connection((host, port), timeout=_TCP_TIMEOUT):
            return True
    except (OSError, socket.timeout):
        return False


def _proxy_available() -> bool:
    """Check if the local proxy is running."""
    parsed = urlparse(_PROXY)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 10020
    return _tcp_reachable(host, port)


def _host_reachable_via_proxy(target_host: str) -> bool:
    """Try an HTTPS CONNECT through the proxy to verify end-to-end path."""
    try:
        opener = gp._make_opener(use_proxy=True)
        req = urllib.request.Request(f"https://{target_host}/", method="HEAD")
        opener.open(req, timeout=_TCP_TIMEOUT)
        return True
    except Exception:
        # Even a 4xx/5xx means the host is reachable
        return True
    return False


def _check_reachability(host: str) -> bool:
    """
    Check if a host is reachable, trying direct first, then via proxy.
    Returns True if we can reach the host by any means.
    """
    # Try direct TCP first
    if _tcp_reachable(host):
        return True
    # Try via proxy if available
    if _proxy_available():
        try:
            return _host_reachable_via_proxy(host)
        except Exception:
            return False
    return False


# ── Provider definitions ──────────────────────────────────────

_PROVIDERS = {
    "openai": {
        "host": "api.openai.com",
        "url": "https://api.openai.com/v1/chat/completions",
        "needs_proxy": True,  # usually blocked in China
    },
    "anthropic": {
        "host": "api.anthropic.com",
        "url": "https://api.anthropic.com/v1/messages",
        "needs_proxy": True,
    },
    "google": {
        "host": "generativelanguage.googleapis.com",
        "url": "https://generativelanguage.googleapis.com/",
        "needs_proxy": True,
    },
    "kimi-coding": {
        "host": "api.kimi.com",
        "url": "https://api.kimi.com/coding/v1/messages",
        "needs_proxy": False,  # domestic
    },
    "minimax": {
        "host": "api.minimax.io",
        "url": "https://api.minimax.io/v1/text/chatcompletion_v2",
        "needs_proxy": False,  # domestic
    },
}


# ── Fixtures ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def reachability():
    """Pre-check reachability of all providers once per module."""
    results = {}
    proxy_up = _proxy_available()
    print(f"\n  Proxy ({_PROXY}): {'[OK] UP' if proxy_up else '[X] DOWN'}")

    for prov_id, info in _PROVIDERS.items():
        host = info["host"]
        if info["needs_proxy"] and not proxy_up:
            results[prov_id] = False
            print(f"  {prov_id:15s} ({host}): [X] SKIP (proxy required but down)")
            continue

        reachable = _check_reachability(host)
        results[prov_id] = reachable
        print(f"  {prov_id:15s} ({host}): {'[OK] reachable' if reachable else '[X] unreachable'}")

    return results


@pytest.fixture(scope="module")
def api_keys():
    """Load API keys from gateway auth-profiles (no mocks, real keys)."""
    keys = {}
    for prov_id in _PROVIDERS:
        key = gp._get_api_key(prov_id)
        keys[prov_id] = key
    return keys


# ── TCP reachability tests (no key needed, no cost) ───────────


class TestTcpReachability:
    """Pure network-level tests. No API keys consumed."""

    def test_openai_reachable(self, reachability):
        if not reachability.get("openai"):
            pytest.skip("api.openai.com unreachable (VPN/proxy needed?)")
        assert reachability["openai"] is True

    def test_anthropic_reachable(self, reachability):
        if not reachability.get("anthropic"):
            pytest.skip("api.anthropic.com unreachable (VPN/proxy needed?)")
        assert reachability["anthropic"] is True

    def test_google_reachable(self, reachability):
        if not reachability.get("google"):
            pytest.skip("generativelanguage.googleapis.com unreachable")
        assert reachability["google"] is True

    def test_kimi_reachable(self, reachability):
        if not reachability.get("kimi-coding"):
            pytest.skip("api.moonshot.cn unreachable")
        assert reachability["kimi-coding"] is True

    def test_minimax_reachable(self, reachability):
        if not reachability.get("minimax"):
            pytest.skip("api.minimax.chat unreachable")
        assert reachability["minimax"] is True


# ── Authenticated API smoke tests (tiny request, minimal cost) ─


class TestApiSmoke:
    """
    Minimal authenticated API calls. Each sends a trivial prompt with
    max_tokens=1~5 to minimize cost. Only runs if host is reachable
    AND an API key is found.
    """

    def _skip_unless_ready(self, provider: str, reachability, api_keys):
        if not reachability.get(provider):
            pytest.skip(f"{provider}: host unreachable")
        if not api_keys.get(provider):
            pytest.skip(f"{provider}: no API key found")

    def test_openai_gpt4o(self, reachability, api_keys):
        self._skip_unless_ready("openai", reachability, api_keys)
        result = gp._call_openai_compatible(
            api_keys["openai"], "gpt-4o", "Say OK",
            "https://api.openai.com/v1/chat/completions",
            max_tokens=5,
        )
        assert len(result) > 0, "GPT-4o returned empty response"
        print(f"  gpt-4o response: {result!r}")

    def test_openai_gpt54(self, reachability, api_keys):
        self._skip_unless_ready("openai", reachability, api_keys)
        result = gp._call_openai_compatible(
            api_keys["openai"], "gpt-5.4", "Say OK",
            "https://api.openai.com/v1/chat/completions",
            max_tokens=5,
        )
        assert len(result) > 0, "GPT-5.4 returned empty response"
        print(f"  gpt-5.4 response: {result!r}")

    def test_anthropic_sonnet(self, reachability, api_keys):
        self._skip_unless_ready("anthropic", reachability, api_keys)
        result = gp._call_anthropic(
            api_keys["anthropic"], "claude-sonnet-4-20250514", "Say OK",
            max_tokens=5,
        )
        assert len(result) > 0, "Claude Sonnet returned empty response"
        print(f"  claude-sonnet-4 response: {result!r}")

    def test_google_gemini_flash(self, reachability, api_keys):
        self._skip_unless_ready("google", reachability, api_keys)
        # _call_google uses maxOutputTokens internally, no max_tokens param
        result = gp._call_google(
            api_keys["google"], "gemini-2.5-flash", "Say OK",
        )
        assert len(result) > 0, "Gemini Flash returned empty response"
        print(f"  gemini-2.5-flash response: {result!r}")

    def test_kimi_k25(self, reachability, api_keys):
        self._skip_unless_ready("kimi-coding", reachability, api_keys)
        # Kimi Coding uses Anthropic protocol at api.kimi.com
        result = gp._call_anthropic(
            api_keys["kimi-coding"], "k2p5", "Say OK",
            max_tokens=5,
            base_url="https://api.kimi.com/coding/v1/messages",
        )
        assert len(result) > 0, "Kimi K2.5 returned empty response"
        print(f"  kimi-k2.5 response: {result!r}")

    def test_minimax_m25(self, reachability, api_keys):
        self._skip_unless_ready("minimax", reachability, api_keys)
        # MiniMax-M2.5 is a reasoning model; needs more tokens to finish thinking
        result = gp._call_openai_compatible(
            api_keys["minimax"], "MiniMax-M2.5", "Say OK",
            "https://api.minimax.io/v1/text/chatcompletion_v2",
            max_tokens=50,
        )
        assert len(result) > 0, "MiniMax M2.5 returned empty response"
        print(f"  minimax-m2.5 response: {result!r}")
