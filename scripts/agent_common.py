"""
Shared utilities for the signal-agent Phase 7 layer.

Centralizes secret loading, Claude/Supabase client factories, retry logic,
logging setup, and per-run state writes so individual agent_*.py scripts
stay focused on their specific job.

All secrets live in ~/workspace/agent-data/state/ at mode 0600. Models and
metadata are picked here so the choice is consistent across scripts.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from anthropic import Anthropic
from supabase import Client, create_client

# ---- paths --------------------------------------------------------------

AGENT_DATA = Path(os.environ.get("AGENT_DATA", str(Path.home() / "workspace" / "agent-data")))
STATE_DIR = AGENT_DATA / "state"
LOG_DIR = AGENT_DATA / "logs"

ANTHROPIC_KEY_PATH = STATE_DIR / "anthropic-api-key"
SUPABASE_URL_PATH = STATE_DIR / "supabase-url"
SUPABASE_KEY_PATH = STATE_DIR / "supabase-service-role-key"

# ---- models -------------------------------------------------------------

# Cheap and fast — used for all bulk work (extraction + indexing).
EXTRACT_MODEL = "claude-haiku-4-5-20251001"
INDEX_MODEL = EXTRACT_MODEL  # alias for callsite clarity

# Used only for synthesis steps where prose quality matters:
# digest narrative, suggest_categories, free-form ask.
SYNTHESIS_MODEL = "claude-opus-4-7"

# Tagged on every API call for usage attribution.
USER_ID = "signal-agent"

SCHEMA = "signal_agent"

# ---- pricing ($ per token; used by CostTracker) -------------------------

# Approximate published rates. Update as Anthropic changes pricing.
PRICES = {
    "claude-haiku-4-5-20251001": {
        "input":       1.00 / 1_000_000,
        "output":      5.00 / 1_000_000,
        "cache_read":  0.10 / 1_000_000,
        "cache_write": 1.25 / 1_000_000,
    },
    "claude-sonnet-4-6": {
        "input":       3.00 / 1_000_000,
        "output":     15.00 / 1_000_000,
        "cache_read":  0.30 / 1_000_000,
        "cache_write": 3.75 / 1_000_000,
    },
    "claude-opus-4-7": {
        "input":      15.00 / 1_000_000,
        "output":     75.00 / 1_000_000,
        "cache_read":  1.50 / 1_000_000,
        "cache_write": 18.75 / 1_000_000,
    },
}


# ---- secrets ------------------------------------------------------------

def _read_secret(path: Path, *, label: str) -> str:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {label} at {path}. See README for setup."
        )
    val = path.read_text(encoding="utf-8").strip()
    if not val:
        raise ValueError(f"{label} at {path} is empty.")
    return val


# ---- client factories ---------------------------------------------------

def get_anthropic_client() -> Anthropic:
    key = _read_secret(ANTHROPIC_KEY_PATH, label="Anthropic API key")
    return Anthropic(api_key=key)


def get_supabase_client() -> Client:
    url = _read_secret(SUPABASE_URL_PATH, label="Supabase URL")
    key = _read_secret(SUPABASE_KEY_PATH, label="Supabase service role key")
    return create_client(url, key)


# ---- agent identity config ---------------------------------------------

AGENT_CONFIG_PATH = Path(__file__).parent / "agent_config.local.json"


def load_agent_config() -> dict:
    """Load identity config (user_full_name, user_first_name, user_emails).

    Used by agent prompts to distinguish self-references from third-party
    mentions. File is gitignored; copy agent_config.example.json to start.
    """
    if not AGENT_CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Missing {AGENT_CONFIG_PATH}. Copy agent_config.example.json "
            "and fill in your name + emails."
        )
    cfg = json.loads(AGENT_CONFIG_PATH.read_text(encoding="utf-8"))
    if not cfg.get("user_full_name"):
        raise ValueError(f"{AGENT_CONFIG_PATH}: user_full_name is required")
    return cfg


# ---- retry --------------------------------------------------------------

def call_claude_with_retry(
    fn: Callable[[], Any],
    *,
    max_retries: int = 4,
    initial_delay: float = 1.0,
) -> Any:
    """Call a Claude API method with exponential backoff on retryable errors.

    `fn` is a zero-arg callable that performs the API call (typically a
    closure over client.messages.create with bound kwargs). Retries on rate
    limits and 5xx errors; raises immediately on 4xx other than 429.
    """
    delay = initial_delay
    last_err: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            last_err = e
            status = getattr(e, "status_code", None)
            err_str = str(e).lower()
            retryable = (
                status == 429
                or (status is not None and 500 <= status < 600)
                or "rate" in err_str
                or "timeout" in err_str
            )
            if not retryable or attempt == max_retries - 1:
                raise
            logging.warning(
                "Claude call retryable error (attempt %d/%d, sleeping %.1fs): %s",
                attempt + 1, max_retries, delay, e,
            )
            time.sleep(delay)
            delay *= 2
    assert last_err is not None
    raise last_err


# ---- logging ------------------------------------------------------------

def setup_logging(name: str) -> Path:
    """Configure root logger to write to logs/<name>.log + stdout. Returns log path."""
    log_path = LOG_DIR / f"{name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    return log_path


# ---- state --------------------------------------------------------------

def write_run_state(name: str, summary: dict) -> Path:
    """Write per-run state JSON under state/<name>.json. Overwrites previous."""
    state_path = STATE_DIR / f"{name}.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return state_path


def read_run_state(name: str) -> Optional[dict]:
    state_path = STATE_DIR / f"{name}.json"
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ---- cost tracking ------------------------------------------------------

class CostBudgetExceeded(Exception):
    """Raised when a planned API call would exceed the run's max-cost cap."""


class CostTracker:
    """Track cumulative Claude API spend and refuse to start work past a cap.

    The cap is a hard ceiling per run; the tracker lets the current call
    finish (we don't have a way to abort mid-call) and refuses subsequent
    calls. With small batch sizes, the overshoot is bounded by the size of
    one batch's call.
    """

    def __init__(self, max_cost: float = 5.0):
        self.max_cost = max_cost
        self.total_cost = 0.0
        self.by_model: dict[str, dict[str, Any]] = {}

    def _ensure(self, model: str) -> dict[str, Any]:
        return self.by_model.setdefault(model, {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cost_usd": 0.0,
        })

    def record(self, model: str, usage: Any) -> float:
        """Record one API call's usage and return the call's cost in USD."""
        prices = PRICES.get(model)
        if prices is None:
            logging.warning("No pricing for model %r; cost not tracked", model)
            return 0.0
        in_tok = getattr(usage, "input_tokens", 0) or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        cache_r = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_w = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cost = (
            in_tok * prices["input"]
            + out_tok * prices["output"]
            + cache_r * prices["cache_read"]
            + cache_w * prices["cache_write"]
        )
        bucket = self._ensure(model)
        bucket["calls"] += 1
        bucket["input_tokens"] += in_tok
        bucket["output_tokens"] += out_tok
        bucket["cache_read_tokens"] += cache_r
        bucket["cache_write_tokens"] += cache_w
        bucket["cost_usd"] += cost
        self.total_cost += cost
        return cost

    def assert_within_budget(self) -> None:
        """Raise CostBudgetExceeded if we've crossed the cap."""
        if self.total_cost >= self.max_cost:
            raise CostBudgetExceeded(
                f"Run cost {self.total_cost:.4f} USD has reached cap "
                f"{self.max_cost:.2f} USD. Aborting before next batch."
            )

    def summary(self) -> dict:
        return {
            "max_cost_usd": self.max_cost,
            "total_cost_usd": round(self.total_cost, 6),
            "by_model": {m: {**b, "cost_usd": round(b["cost_usd"], 6)} for m, b in self.by_model.items()},
        }


# ---- prompt caching helper ----------------------------------------------

def cached_system(text: str) -> list[dict]:
    """Wrap a system-prompt string in a single cacheable block.

    Pass the result as the `system=` argument to messages.create. Subsequent
    calls within ~5 minutes get a 90% discount on the cached prefix.
    """
    return [
        {
            "type": "text",
            "text": text,
            "cache_control": {"type": "ephemeral"},
        }
    ]
