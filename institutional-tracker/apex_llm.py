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

# Load .env HERE, not just in config.py. Cron jobs have no shell profile, so any
# entry point that called apex_llm without importing config first saw an empty
# ANTHROPIC_API_KEY and every LLM call silently returned None (usage log froze on
# 2026-06-12 because of exactly this). Loading at import makes the key available
# to all ~36 enabled agents regardless of entry point.
try:
    from dotenv import load_dotenv as _ld
    _ld(dotenv_path=Path(__file__).parent / ".env", override=False)
except Exception:
    pass

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
            "model": "claude-fable-5",
            "fallback_model": "claude-opus-4-8",
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
    return _load_config().get("model", "claude-fable-5")


_USAGE_PATH = Path(__file__).parent / "data" / "llm_usage.json"


def _load_usage() -> dict:
    try:
        if _USAGE_PATH.exists():
            return json.loads(_USAGE_PATH.read_text())
    except Exception:
        pass
    return {}


def _record_usage(agent_name: str, in_tok: int, out_tok: int) -> None:
    """Track daily call counts and token spend per agent (data/llm_usage.json)."""
    try:
        from datetime import date
        usage = _load_usage()
        today = str(date.today())
        if usage.get("date") != today:
            usage = {"date": today, "calls": 0, "input_tokens": 0,
                     "output_tokens": 0, "by_agent": {}}
        usage["calls"] += 1
        usage["input_tokens"]  += in_tok
        usage["output_tokens"] += out_tok
        agent = agent_name or "unknown"
        a = usage["by_agent"].setdefault(agent, {"calls": 0, "in": 0, "out": 0})
        a["calls"] += 1
        a["in"]  += in_tok
        a["out"] += out_tok
        _USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _USAGE_PATH.write_text(json.dumps(usage, indent=2))
    except Exception as e:
        logger.debug("[LLM] Usage tracking failed: %s", e)


def _budget_exceeded(cfg: dict) -> bool:
    """Daily call cap — protects the credit balance from runaway loops."""
    cap = cfg.get("max_daily_calls", 500)
    try:
        from datetime import date
        usage = _load_usage()
        return usage.get("date") == str(date.today()) and usage.get("calls", 0) >= cap
    except Exception:
        return False


def get_usage_today() -> dict:
    """Return today's usage stats for dashboards/reports."""
    from datetime import date
    usage = _load_usage()
    if usage.get("date") != str(date.today()):
        return {"date": str(date.today()), "calls": 0, "input_tokens": 0,
                "output_tokens": 0, "by_agent": {}}
    return usage


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

    if _budget_exceeded(cfg):
        logger.warning("[LLM] Daily call budget reached — falling back to rules")
        return None

    model     = cfg.get("model", "claude-fable-5")
    fallback  = cfg.get("fallback_model")
    # Floor of 600: Fable 5's thinking tokens count against max_tokens, so a
    # tight cap can truncate before any text is emitted (worst case ~3¢).
    n_tokens  = max(max_tokens or cfg.get("max_tokens", 1024), 600)
    timeout   = cfg.get("timeout_seconds", 30)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
    except Exception as e:
        logger.warning("[LLM] client init failed: %s", e)
        return None

    def _call(m):
        msg = client.messages.create(
            model=m, max_tokens=n_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), "")
        _record_usage(agent_name, getattr(msg.usage, "input_tokens", 0),
                      getattr(msg.usage, "output_tokens", 0))
        return text

    # Try the primary model, then the configured fallback_model on a transient
    # failure (e.g. Fable temporarily unavailable) before giving up to rule-based
    # logic. Auth errors never retry — the key is the problem.
    candidates = [model] + ([fallback] if fallback and fallback != model else [])
    for i, m in enumerate(candidates):
        try:
            text = _call(m)
            if i > 0:
                logger.warning("[LLM] primary failed; served by fallback %s", m)
            logger.debug("[LLM] %s (%s) → %d chars", agent_name or "?", m, len(text))
            return text
        except anthropic.AuthenticationError:
            logger.warning("[LLM] Invalid API key — check ANTHROPIC_API_KEY")
            return None
        except anthropic.RateLimitError:
            logger.warning("[LLM] Rate limit on %s%s", m, " — trying fallback" if i == 0 and len(candidates) > 1 else "")
        except Exception as e:
            logger.warning("[LLM] Call failed (%s): %s%s", m, e, " — trying fallback" if i == 0 and len(candidates) > 1 else "")
    return None
