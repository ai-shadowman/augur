import logging
import os
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def estimate_cost(model: Optional[str], prompt_tokens: int = 0, completion_tokens: int = 0) -> float:
    """Estimates cost in USD for the given model and token counts.

    Attempts to use LiteLLM's internal cost database first, falling back to
    standard published pricing tables for major foundation models.
    """
    p = int(prompt_tokens or 0)
    c = int(completion_tokens or 0)
    if p == 0 and c == 0:
        return 0.0

    clean_model = model or ""

    # 1. Try LiteLLM's built-in cost database
    if clean_model:
        try:
            import litellm
            res = litellm.cost_per_token(model=clean_model, prompt_tokens=p, completion_tokens=c)
            if isinstance(res, (tuple, list)) and len(res) >= 2:
                cost = float(res[0]) + float(res[1])
                if cost > 0:
                    return cost
            elif isinstance(res, (float, int)) and res > 0:
                return float(res)
        except Exception:
            pass

    # 2. Heuristic pricing tables (rates per 1,000,000 tokens: (prompt_rate, completion_rate))
    model_str = clean_model.lower()
    RATES = {
        "gpt-4o-mini": (0.15, 0.60),
        "gpt-4o": (2.50, 10.00),
        "gpt-4-turbo": (10.00, 30.00),
        "gpt-4": (30.00, 60.00),
        "gpt-3.5": (0.50, 1.50),
        "claude-3-5-sonnet": (3.00, 15.00),
        "claude-3-5-haiku": (0.80, 4.00),
        "claude-3-haiku": (0.25, 1.25),
        "claude-3-opus": (15.00, 75.00),
        "gemini-1.5-flash": (0.075, 0.30),
        "gemini-1.5-pro": (1.25, 5.00),
        "gemini-2.0-flash": (0.10, 0.40),
        "llama-3": (0.20, 0.20),
        "mistral": (0.20, 0.60),
    }

    for key, (p_rate, c_rate) in RATES.items():
        if key in model_str:
            return (p * p_rate + c * c_rate) / 1_000_000.0

    # Default baseline estimation: $2.00 / 1M prompt tokens, $8.00 / 1M output tokens
    return (p * 2.00 + c * 8.00) / 1_000_000.0


class TokenTracker:
    """Thread-safe tracker for LLM token usage and cost across the application."""

    def __init__(self):
        self._lock = threading.Lock()
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._total_tokens = 0
        self._total_cost = 0.0
        self._call_count = 0
        self._model_breakdown: Dict[str, Dict[str, Any]] = {}

    def track(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        cost: Optional[float] = None,
        model: Optional[str] = None,
        stage: Optional[str] = None,
    ) -> None:
        """Records token usage and cost for an LLM call.

        Args:
            prompt_tokens: Number of prompt/input tokens.
            completion_tokens: Number of completion/output tokens.
            total_tokens: Total tokens. If 0 or omitted, computed as prompt + completion.
            cost: Optional cost in USD. If None or 0, estimated automatically.
            model: Optional model name or provider identifier.
            stage: Optional pipeline stage name (e.g., 'data_generation', 'analysis').
        """
        p = int(prompt_tokens or 0)
        c = int(completion_tokens or 0)
        t = int(total_tokens or 0)
        if t == 0 and (p > 0 or c > 0):
            t = p + c
        elif t > 0 and p == 0 and c == 0:
            p = t

        call_cost = float(cost) if cost is not None and cost > 0 else estimate_cost(model, p, c)

        model_key = model or "unknown"
        if stage:
            model_key = f"{stage} ({model_key})"

        with self._lock:
            self._prompt_tokens += p
            self._completion_tokens += c
            self._total_tokens += t
            self._total_cost += call_cost
            self._call_count += 1

            if model_key not in self._model_breakdown:
                self._model_breakdown[model_key] = {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cost": 0.0,
                }
            self._model_breakdown[model_key]["calls"] += 1
            self._model_breakdown[model_key]["prompt_tokens"] += p
            self._model_breakdown[model_key]["completion_tokens"] += c
            self._model_breakdown[model_key]["total_tokens"] += t
            self._model_breakdown[model_key]["cost"] += call_cost

    def get_total_tokens(self) -> int:
        """Returns the aggregate total number of tokens used."""
        with self._lock:
            return self._total_tokens

    def get_total_cost(self) -> float:
        """Returns the aggregate total estimated cost in USD."""
        with self._lock:
            return self._total_cost

    def get_summary(self) -> Dict[str, Any]:
        """Returns a snapshot dictionary of token usage and cost statistics."""
        with self._lock:
            return {
                "call_count": self._call_count,
                "prompt_tokens": self._prompt_tokens,
                "completion_tokens": self._completion_tokens,
                "total_tokens": self._total_tokens,
                "total_cost": self._total_cost,
                "breakdown": {k: dict(v) for k, v in self._model_breakdown.items()},
            }

    def reset(self) -> None:
        """Resets all token and cost counters to zero."""
        with self._lock:
            self._prompt_tokens = 0
            self._completion_tokens = 0
            self._total_tokens = 0
            self._total_cost = 0.0
            self._call_count = 0
            self._model_breakdown.clear()

    def format_summary(self) -> str:
        """Formats token and cost statistics into a clean text block."""
        summary = self.get_summary()
        lines = [
            "",
            "=" * 78,
            "                      LLM TOKEN USAGE & COST SUMMARY",
            "=" * 78,
            f" Total LLM Invocations : {summary['call_count']:,}",
            f" Total Prompt Tokens   : {summary['prompt_tokens']:,}",
            f" Total Output Tokens   : {summary['completion_tokens']:,}",
            f" Total Tokens Used     : {summary['total_tokens']:,}",
            f" Estimated Total Cost  : ${summary['total_cost']:.4f}",
        ]

        if summary["breakdown"]:
            lines.append("-" * 78)
            lines.append(
                f" {'Source / Model':<32} {'Calls':<7} {'Prompt':<10} {'Output':<10} {'Total':<10} {'Est. Cost':<10}"
            )
            lines.append("-" * 78)
            for name, stats in summary["breakdown"].items():
                truncated_name = (name[:29] + "...") if len(name) > 32 else name
                cost_str = f"${stats['cost']:.4f}"
                lines.append(
                    f" {truncated_name:<32} "
                    f"{stats['calls']:<7} "
                    f"{stats['prompt_tokens']:<10,} "
                    f"{stats['completion_tokens']:<10,} "
                    f"{stats['total_tokens']:<10,} "
                    f"{cost_str:<10}"
                )

        lines.append("=" * 78)
        lines.append("")
        return "\n".join(lines)

    def display_summary(self) -> None:
        """Prints and logs the formatted token and cost usage summary."""
        formatted = self.format_summary()
        print(formatted, flush=True)
        logger.info(
            f"LLM Token & Cost Summary: {self._total_tokens} total tokens (${self._total_cost:.4f}) across {self._call_count} calls."
        )


# Global singleton instance
_GLOBAL_TRACKER = TokenTracker()
_LITELLM_INITIALIZED = False
_INIT_LOCK = threading.Lock()


def track_tokens(
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    cost: Optional[float] = None,
    model: Optional[str] = None,
    stage: Optional[str] = None,
) -> None:
    """Records token usage and cost in the global tracker."""
    _GLOBAL_TRACKER.track(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost=cost,
        model=model,
        stage=stage,
    )


def get_total_tokens() -> int:
    """Returns the total number of tokens recorded so far."""
    return _GLOBAL_TRACKER.get_total_tokens()


def get_total_cost() -> float:
    """Returns the total estimated cost recorded so far."""
    return _GLOBAL_TRACKER.get_total_cost()


def get_token_summary() -> Dict[str, Any]:
    """Returns the full token summary dictionary."""
    return _GLOBAL_TRACKER.get_summary()


def reset_token_count() -> None:
    """Resets the global token tracker."""
    _GLOBAL_TRACKER.reset()


def display_token_summary() -> None:
    """Prints the formatted token summary to stdout and logs it."""
    _GLOBAL_TRACKER.display_summary()


def setup_litellm_token_tracking() -> None:
    """Hooks into LiteLLM's success callbacks to automatically capture token and cost metrics from all calls."""
    global _LITELLM_INITIALIZED
    with _INIT_LOCK:
        if _LITELLM_INITIALIZED:
            return

        try:
            import litellm

            def _litellm_success_callback(kwargs, completion_response, start_time, end_time):
                try:
                    usage = getattr(completion_response, "usage", None)
                    if usage is not None:
                        prompt_toks = getattr(usage, "prompt_tokens", 0) or 0
                        completion_toks = getattr(usage, "completion_tokens", 0) or 0
                        total_toks = getattr(usage, "total_tokens", 0) or 0
                    elif isinstance(completion_response, dict) and "usage" in completion_response:
                        u = completion_response["usage"] or {}
                        prompt_toks = u.get("prompt_tokens", 0) or 0
                        completion_toks = u.get("completion_tokens", 0) or 0
                        total_toks = u.get("total_tokens", 0) or 0
                    else:
                        prompt_toks = 0
                        completion_toks = 0
                        total_toks = 0

                    # Try to extract exact cost from LiteLLM
                    call_cost = None
                    try:
                        if hasattr(litellm, "completion_cost"):
                            call_cost = litellm.completion_cost(completion_response=completion_response)
                    except Exception:
                        call_cost = None

                    model = kwargs.get("model") if isinstance(kwargs, dict) else getattr(kwargs, "model", "litellm")
                    track_tokens(
                        prompt_tokens=prompt_toks,
                        completion_tokens=completion_toks,
                        total_tokens=total_toks,
                        cost=call_cost,
                        model=str(model),
                    )
                except Exception as ex:
                    logger.debug(f"Error in litellm token tracking callback: {ex}")

            if not hasattr(litellm, "success_callback") or litellm.success_callback is None:
                litellm.success_callback = []

            if _litellm_success_callback not in litellm.success_callback:
                litellm.success_callback.append(_litellm_success_callback)

            _LITELLM_INITIALIZED = True
            logger.debug("LiteLLM automatic token tracking callback registered successfully.")

        except ImportError:
            logger.debug("LiteLLM is not installed. Automatic LiteLLM token tracking disabled.")
        except Exception as e:
            logger.warning(f"Failed to setup LiteLLM token tracking: {e}")
