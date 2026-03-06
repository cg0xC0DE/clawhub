"""Tests for generate_persona.py — persona generation logic."""

import json
import sys
import urllib.request
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import generate_persona as gp


# ── _parse_three_files ────────────────────────────────────────


class TestParseThreeFiles:
    def test_all_three_markers(self):
        text = (
            "===SOUL.MD===\n# Soul content here\nLine 2\n"
            "===IDENTITY.MD===\n# Identity content\n"
            "===AGENTS.MD===\n# Agents content\n"
        )
        result = gp._parse_three_files(text)
        assert len(result) == 3
        assert "SOUL.md" in result
        assert "IDENTITY.md" in result
        assert "AGENTS.md" in result
        assert "Soul content" in result["SOUL.md"]
        assert "Identity content" in result["IDENTITY.md"]
        assert "Agents content" in result["AGENTS.md"]

    def test_missing_one_marker(self):
        text = (
            "===SOUL.MD===\n# Soul\n"
            "===AGENTS.MD===\n# Agents\n"
        )
        result = gp._parse_three_files(text)
        assert len(result) == 2
        assert "SOUL.md" in result
        assert "AGENTS.md" in result
        assert "IDENTITY.md" not in result

    def test_empty_content_after_marker(self):
        text = "===SOUL.MD===\n\n===IDENTITY.MD===\ncontent\n===AGENTS.MD===\n\n"
        result = gp._parse_three_files(text)
        assert "SOUL.md" not in result  # empty after stripping
        assert "IDENTITY.md" in result
        assert "AGENTS.md" not in result

    def test_no_markers(self):
        result = gp._parse_three_files("just some random text")
        assert result == {}

    def test_extra_text_before_markers(self):
        text = (
            "Here is the output:\n\n"
            "===SOUL.MD===\nSoul\n"
            "===IDENTITY.MD===\nIdentity\n"
            "===AGENTS.MD===\nAgents\n"
        )
        result = gp._parse_three_files(text)
        assert len(result) == 3

    def test_multiline_content(self):
        text = (
            "===SOUL.MD===\n"
            "# SOUL.md - 孙子\n\n"
            "_你不是程序。_\n\n"
            "## 你是谁\n\n"
            "我是孙武。\n"
            "===IDENTITY.MD===\n"
            "# IDENTITY\n"
            "===AGENTS.MD===\n"
            "# AGENTS\n"
        )
        result = gp._parse_three_files(text)
        assert "你不是程序" in result["SOUL.md"]
        assert "孙武" in result["SOUL.md"]

    def test_case_sensitivity_of_markers(self):
        text = "===soul.md===\nContent"
        result = gp._parse_three_files(text)
        assert result == {}  # markers must be uppercase


# ── MODEL_REGISTRY ────────────────────────────────────────────


class TestModelRegistry:
    def test_all_models_have_required_fields(self):
        for key, info in gp.MODEL_REGISTRY.items():
            assert "provider" in info, f"{key} missing provider"
            assert "model_id" in info, f"{key} missing model_id"
            assert "label" in info, f"{key} missing label"

    def test_default_model_exists(self):
        assert gp.DEFAULT_MODEL in gp.MODEL_REGISTRY

    def test_all_providers_have_urls(self):
        providers = {info["provider"] for info in gp.MODEL_REGISTRY.values()}
        for prov in providers:
            assert prov in gp._PROVIDER_URLS, f"provider {prov} has no URL"


# ── _call_llm routing ────────────────────────────────────────


class TestCallLlmRouting:
    def test_unknown_model_raises(self):
        with pytest.raises(ValueError, match="Unknown model"):
            gp._call_llm("test", model="nonexistent-model")

    @patch.object(gp, "_get_api_key", return_value="")
    def test_no_api_key_raises(self, mock_key):
        with pytest.raises(RuntimeError, match="No API key"):
            gp._call_llm("test", model="gpt-5.4")

    @patch.object(gp, "_get_api_key", return_value="sk-test")
    @patch.object(gp, "_call_anthropic", return_value="response")
    def test_anthropic_routing(self, mock_call, mock_key):
        result = gp._call_llm("test", model="claude-sonnet-4.6")
        mock_call.assert_called_once()
        assert result == "response"

    @patch.object(gp, "_get_api_key", return_value="sk-test")
    @patch.object(gp, "_call_google", return_value="response")
    def test_google_routing(self, mock_call, mock_key):
        result = gp._call_llm("test", model="gemini-3.1-pro")
        mock_call.assert_called_once()
        assert result == "response"

    @patch.object(gp, "_get_api_key", return_value="sk-test")
    @patch.object(gp, "_call_openai_compatible", return_value="response")
    def test_openai_routing(self, mock_call, mock_key):
        result = gp._call_llm("test", model="gpt-5.4")
        mock_call.assert_called_once()
        assert result == "response"

    @patch.object(gp, "_get_api_key", return_value="sk-test")
    @patch.object(gp, "_call_anthropic", return_value="response")
    def test_kimi_routing(self, mock_call, mock_key):
        result = gp._call_llm("test", model="kimi-k2.5")
        mock_call.assert_called_once()
        # Kimi Coding uses Anthropic protocol at api.kimi.com
        assert "kimi.com" in mock_call.call_args.kwargs.get("base_url", "")


# ── get_available_models ──────────────────────────────────────


class TestGetAvailableModels:
    @patch.object(gp, "_get_api_key")
    def test_filters_by_key_availability(self, mock_key):
        mock_key.side_effect = lambda prov: "key" if prov == "openai" else ""
        models = gp.get_available_models()
        providers = {m["provider"] for m in models}
        assert "openai" in providers
        assert "anthropic" not in providers

    @patch.object(gp, "_get_api_key", return_value="")
    def test_no_keys_returns_empty(self, mock_key):
        models = gp.get_available_models()
        assert models == []

    @patch.object(gp, "_get_api_key", return_value="key")
    def test_all_keys_returns_all(self, mock_key):
        models = gp.get_available_models()
        assert len(models) == len(gp.MODEL_REGISTRY)
        for m in models:
            assert "key" in m
            assert "label" in m
            assert "provider" in m


# ── _make_opener ──────────────────────────────────────────────


class TestCallOpenaiCompatiblePayload:
    """Verify the actual payload sent to the API — this catches the max_tokens vs
    max_completion_tokens issue that broke GPT-5.4."""

    @patch.object(gp, "_make_opener")
    def test_openai_uses_max_completion_tokens(self, mock_opener):
        """OpenAI API should use max_completion_tokens for newer models."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": "ok"}}]
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_opener.return_value.open.return_value = mock_resp

        gp._call_openai_compatible("sk-test", "gpt-5.4", "hello",
                                   "https://api.openai.com/v1/chat/completions")

        call_args = mock_opener.return_value.open.call_args
        req = call_args[0][0]
        body = json.loads(req.data.decode())
        assert "max_completion_tokens" in body
        assert "max_tokens" not in body

    @patch.object(gp, "_make_opener")
    def test_kimi_uses_max_tokens(self, mock_opener):
        """Non-OpenAI providers should still use max_tokens."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": "ok"}}]
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_opener.return_value.open.return_value = mock_resp

        gp._call_openai_compatible("sk-test", "kimi-k2.5", "hello",
                                   "https://api.moonshot.cn/v1/chat/completions")

        call_args = mock_opener.return_value.open.call_args
        req = call_args[0][0]
        body = json.loads(req.data.decode())
        assert "max_tokens" in body
        assert "max_completion_tokens" not in body

    @patch.object(gp, "_make_opener")
    def test_minimax_uses_max_tokens(self, mock_opener):
        """MiniMax should also use max_tokens."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": "ok"}}]
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_opener.return_value.open.return_value = mock_resp

        gp._call_openai_compatible("sk-test", "MiniMax-M1", "hello",
                                   "https://api.minimax.chat/v1/text/chatcompletion_v2")

        call_args = mock_opener.return_value.open.call_args
        req = call_args[0][0]
        body = json.loads(req.data.decode())
        assert "max_tokens" in body
        assert "max_completion_tokens" not in body


class TestMakeOpener:
    def test_with_proxy(self):
        opener = gp._make_opener(use_proxy=True)
        handlers = [type(h).__name__ for h in opener.handlers]
        assert "ProxyHandler" in handlers

    def test_without_proxy(self):
        opener = gp._make_opener(use_proxy=False)
        # build_opener() always includes a default ProxyHandler,
        # but the one without use_proxy should NOT have our custom PROXY URLs
        for h in opener.handlers:
            if isinstance(h, urllib.request.ProxyHandler):
                proxies = getattr(h, "proxies", {})
                assert gp.PROXY not in proxies.values()
                break


# ── generate_persona (integration with mocked LLM) ───────────


class TestGeneratePersona:
    def test_skip_existing_soul(self, tmp_path):
        """Should skip when SOUL.md already has death-transmigration content."""
        with patch.object(gp, "GATEWAYS_DIR", tmp_path):
            ws = tmp_path / "test_agent" / "state" / "workspace"
            ws.mkdir(parents=True)
            (ws / "SOUL.md").write_text("## 你是谁\n前世之魂", encoding="utf-8")

            result = gp.generate_persona("test_agent", "测试", force=False)
            assert result == {}  # skipped

    def test_force_overrides_skip(self, tmp_path):
        """force=True should regenerate even if SOUL.md exists."""
        llm_output = (
            "===SOUL.MD===\nNew soul\n"
            "===IDENTITY.MD===\nNew identity\n"
            "===AGENTS.MD===\nNew agents\n"
        )
        with patch.object(gp, "GATEWAYS_DIR", tmp_path), \
             patch.object(gp, "_call_llm", return_value=llm_output), \
             patch.object(gp, "_build_prompt", return_value="prompt"):
            ws = tmp_path / "test_agent" / "state" / "workspace"
            ws.mkdir(parents=True)
            (ws / "SOUL.md").write_text("## 你是谁\n旧内容", encoding="utf-8")

            result = gp.generate_persona("test_agent", "测试", force=True)
            assert len(result) == 3
            assert "New soul" in (ws / "SOUL.md").read_text(encoding="utf-8")

    def test_creates_workspace_if_missing(self, tmp_path):
        llm_output = (
            "===SOUL.MD===\nSoul\n"
            "===IDENTITY.MD===\nIdentity\n"
            "===AGENTS.MD===\nAgents\n"
        )
        with patch.object(gp, "GATEWAYS_DIR", tmp_path), \
             patch.object(gp, "_call_llm", return_value=llm_output), \
             patch.object(gp, "_build_prompt", return_value="prompt"):
            result = gp.generate_persona("new_agent", "新角色", force=True)
            assert len(result) == 3
            ws = tmp_path / "new_agent" / "state" / "workspace"
            assert ws.exists()
            assert (ws / "SOUL.md").exists()

    def test_backup_existing_files(self, tmp_path):
        """Existing files should be backed up to .bak.prev."""
        llm_output = (
            "===SOUL.MD===\nNew\n"
            "===IDENTITY.MD===\nNew\n"
            "===AGENTS.MD===\nNew\n"
        )
        with patch.object(gp, "GATEWAYS_DIR", tmp_path), \
             patch.object(gp, "_call_llm", return_value=llm_output), \
             patch.object(gp, "_build_prompt", return_value="prompt"):
            ws = tmp_path / "agent" / "state" / "workspace"
            ws.mkdir(parents=True)
            (ws / "SOUL.md").write_text("Old soul", encoding="utf-8")

            gp.generate_persona("agent", "角色", force=True)
            assert (ws / "SOUL.md.bak.prev").exists()
            assert (ws / "SOUL.md.bak.prev").read_text(encoding="utf-8") == "Old soul"

    def test_llm_error_propagates(self, tmp_path):
        """LLM errors should propagate as exceptions."""
        with patch.object(gp, "GATEWAYS_DIR", tmp_path), \
             patch.object(gp, "_call_llm", side_effect=RuntimeError("API error")), \
             patch.object(gp, "_build_prompt", return_value="prompt"):
            ws = tmp_path / "agent" / "state" / "workspace"
            ws.mkdir(parents=True)

            with pytest.raises(RuntimeError, match="API error"):
                gp.generate_persona("agent", "角色", force=True)

    def test_partial_parse_returns_partial(self, tmp_path):
        """If only 2 of 3 files parsed, should still return those 2."""
        llm_output = "===SOUL.MD===\nSoul\n===AGENTS.MD===\nAgents\n"
        with patch.object(gp, "GATEWAYS_DIR", tmp_path), \
             patch.object(gp, "_call_llm", return_value=llm_output), \
             patch.object(gp, "_build_prompt", return_value="prompt"):
            ws = tmp_path / "agent" / "state" / "workspace"
            ws.mkdir(parents=True)

            result = gp.generate_persona("agent", "角色", force=True)
            assert len(result) == 2
            assert "SOUL.md" in result
            assert "AGENTS.md" in result


# ── _get_api_key ──────────────────────────────────────────────


class TestGetApiKey:
    def test_finds_key_from_auth_profiles(self, tmp_path):
        with patch.object(gp, "GATEWAYS_DIR", tmp_path):
            auth_dir = tmp_path / "gw1" / "state" / "agents" / "main" / "agent"
            auth_dir.mkdir(parents=True)
            auth_data = {
                "profiles": {
                    "openai:default": {"provider": "openai", "key": "sk-test123"},
                }
            }
            (auth_dir / "auth-profiles.json").write_text(
                json.dumps(auth_data), encoding="utf-8"
            )

            key = gp._get_api_key("openai")
            assert key == "sk-test123"

    def test_returns_empty_when_no_key(self, tmp_path):
        with patch.object(gp, "GATEWAYS_DIR", tmp_path):
            key = gp._get_api_key("openai")
            assert key == ""

    def test_skips_profiles_without_key(self, tmp_path):
        with patch.object(gp, "GATEWAYS_DIR", tmp_path):
            auth_dir = tmp_path / "gw1" / "state" / "agents" / "main" / "agent"
            auth_dir.mkdir(parents=True)
            auth_data = {
                "profiles": {
                    "openai:default": {"provider": "openai"},  # no key field
                }
            }
            (auth_dir / "auth-profiles.json").write_text(
                json.dumps(auth_data), encoding="utf-8"
            )

            key = gp._get_api_key("openai")
            assert key == ""
