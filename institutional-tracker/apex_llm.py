#!/usr/bin/env python3
"""
APEX CAPITAL — CENTRAL LLM INTERFACE
======================================
Single place to swap models. When Fable access expires (June 22, 2026),
change "model" in config/llm_config.json — nothing else needs to change.

Usage
-----
    from apex_llm import reason, is_available

    result = reason(
        system="You are the R&D Investment Director...",
        user="Should we approve this proposal: ...",
        agent_name="rd_pod_board",   # checked against enabled_agents
    )
    # result is a plain string — the model's response

    if not is_available("rd_pod_board"):
        # fall back to rule-based logic
        ...

Model swap after June 22
------------------------
    Edit config/llm_config.json:
        "model": "claude-opus-4-8"   ← one line change
    Everything else is identical.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("apex_llm")

_CONFIG_PATH = Path(__file__).parent / "config" / "llm_config.json"
_config: Optional[dict] = None


def _load_config() -> dict:
    global _config
    if _config is not None:
        return _config
    try:
        with open(_CONFIG_PATH) as f:
            _config = json.load(f)
    except Exception as e:
        logger.warning("[LLM] Config load failed: %s — using defaults", e)
        _config = {
            "model": "claude-sonnet-4-6",
            "fallback_model": "claude-sonnet-4-6",
            "api_key_env": "ANTHROPIC_API_KEY",
            "max_tokens": 1024,
            "timeout_seconds": 30,
            "enabled_agents": [],
        }
    return _config


def is_available(agent_name: str = "") -> bool:
    """Return True if LLM reasoning is configured and the API key is set."""
    cfg = _load_config()
    if agent_name and agent_name not in cfg.get("enabled_agents", []):
        return False
    api_key = os.environ.get(cfg.get("api_key_env", "ANTHROPIC_API_KEY"), "")
    return bool(api_key)


def get_model() -> str:
    return _load_config().get("model", "claude-sonnet-4-6")


def reason(
    system: str,
    user: str,
    agent_name: str = "",
    max_tokens: Optional[int] = None,
) -> Optional[str]:
    """
    Call the configured LLM and return the text response.
    Returns None on any failure — callers must fall back to rule-based logic.
    """
    cfg = _load_config()

    if agent_name and agent_name not in cfg.get("enabled_agents", []):
        logger.debug("[LLM] Agent '%s' not in enabled_agents — skipping", agent_name)
        return None

    api_key = os.environ.get(cfg.get("api_key_env", "ANTHROPIC_API_KEY"), "")
    if not api_key:
        logger.debug("[LLM] No API key set — falling back to rule-based logic")
        return None

    model     = cfg.get("model", "claude-sonnet-4-6")
    n_tokens  = max_tokens or cfg.get("max_tokens", 1024)
    timeout   = cfg.get("timeout_seconds", 30)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
        msg = client.messages.create(
            model=model,
            max_tokens=n_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = msg.content[0].text if msg.content else ""
        logger.debug("[LLM] %s (%s) → %d chars", agent_name or "?", model, len(text))
        return text
    except anthropic.AuthenticationError:
        logger.warning("[LLM] Invalid API key — check ANTHROPIC_API_KEY")
    except anthropic.RateLimitError:
        logger.warning("[LLM] Rate limit hit — falling back")
    except Exception as e:
        logger.warning("[LLM] Call failed (%s): %s — falling back", model, e)
    return None
