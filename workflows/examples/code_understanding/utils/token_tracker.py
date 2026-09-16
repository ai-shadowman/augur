import os
import json
import logging
from typing import Optional, Dict, Any

try:
    import litellm
    HAS_LITELLM = True
except ImportError:
    litellm = None
    HAS_LITELLM = False


class TokenCostTracker:
    """Tracks token usage and estimates LLM invocation costs across a pipeline using LiteLLM."""

    # Default pricing:
    # Chat: $2.00 / 1M prompt tokens ($0.000002/token), $8.00 / 1M output tokens ($0.000008/token)
    # Embedding: $0.20 / 1M prompt tokens ($0.0000002/token), $0.00 output tokens
    DEFAULT_CHAT_PROMPT_PRICE = float(os.getenv("CHAT_PRICE_PER_PROMPT_TOKEN", "0.000002"))
    DEFAULT_CHAT_OUTPUT_PRICE = float(os.getenv("CHAT_PRICE_PER_OUTPUT_TOKEN", "0.000008"))
    DEFAULT_EMBED_PROMPT_PRICE = float(os.getenv("EMBED_PRICE_PER_PROMPT_TOKEN", "0.0000002"))
    DEFAULT_EMBED_OUTPUT_PRICE = 0.0

    _global_instance: Optional["TokenCostTracker"] = None

    @classmethod
    def get_instance(cls) -> "TokenCostTracker":
        """Returns the shared global TokenCostTracker instance."""
        if cls._global_instance is None:
            cls._global_instance = cls()
        return cls._global_instance

    @classmethod
    def reset_instance(cls) -> "TokenCostTracker":
        """Resets and returns the singleton instance."""
        cls._global_instance = cls()
        return cls._global_instance

    def __init__(
        self,
        chat_model: Optional[str] = None,
        embed_model: Optional[str] = None,
        chat_prompt_price: Optional[float] = None,
        chat_output_price: Optional[float] = None,
        embed_prompt_price: Optional[float] = None,
    ):
        self.chat_model = chat_model or os.getenv("GRAPHRAG_LLM_ID", "openai/gpt-oss-120b")
        self.embed_model = embed_model or os.getenv("EMBED_LLM_ID", "e5-mistral-7b-instruct")

        self.chat_prompt_price = (
            chat_prompt_price if chat_prompt_price is not None else self.DEFAULT_CHAT_PROMPT_PRICE
        )
        self.chat_output_price = (
            chat_output_price if chat_output_price is not None else self.DEFAULT_CHAT_OUTPUT_PRICE
        )
        self.embed_prompt_price = (
            embed_prompt_price if embed_prompt_price is not None else self.DEFAULT_EMBED_PROMPT_PRICE
        )
        self.embed_output_price = self.DEFAULT_EMBED_OUTPUT_PRICE

        # Registered usage entries: dict of source_label -> metrics dict
        self.records: Dict[str, Dict[str, Any]] = {}

        self._register_models_in_litellm()

    def _register_models_in_litellm(self):
        """Registers custom model pricing with LiteLLM if available."""
        if not HAS_LITELLM or litellm is None:
            return

        model_dict = {
            self.chat_model: {
                "input_cost_per_token": self.chat_prompt_price,
                "output_cost_per_token": self.chat_output_price,
                "litellm_provider": "openai",
            },
            self.embed_model: {
                "input_cost_per_token": self.embed_prompt_price,
                "output_cost_per_token": self.embed_output_price,
                "litellm_provider": "openai",
            },
        }

        try:
            if hasattr(litellm, "register_model"):
                litellm.register_model(model_dict)
            if hasattr(litellm, "model_cost") and isinstance(litellm.model_cost, dict):
                litellm.model_cost.update(model_dict)
        except Exception as e:
            logging.debug(f"Failed to register models in litellm: {e}")

    def count_tokens(self, text: str, model: Optional[str] = None) -> int:
        """Counts tokens for the provided text using LiteLLM."""
        if not text:
            return 0

        target_model = model or self.chat_model

        if HAS_LITELLM and litellm is not None:
            try:
                # Use litellm token counter
                val = litellm.token_counter(model=target_model, text=text)
                if isinstance(val, (int, float)):
                    return int(val)
            except Exception as e:
                logging.debug(f"litellm.token_counter error for model {target_model}: {e}")

        # Fallback approximation: ~4 characters per token
        return max(1, len(text) // 4)

    def calculate_cost(
        self, prompt_tokens: int, output_tokens: int = 0, model: Optional[str] = None
    ) -> float:
        """Calculates estimated cost for prompt and output tokens using LiteLLM."""
        target_model = model or self.chat_model

        if HAS_LITELLM and litellm is not None:
            try:
                prompt_cost, completion_cost = litellm.cost_per_token(
                    model=target_model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=output_tokens,
                )
                return float(prompt_cost + completion_cost)
            except Exception as e:
                logging.debug(f"litellm.cost_per_token error for model {target_model}: {e}")

        # Fallback direct calculation
        is_embed = (target_model == self.embed_model) or ("embed" in target_model.lower())
        if is_embed:
            return prompt_tokens * self.embed_prompt_price
        else:
            return (prompt_tokens * self.chat_prompt_price) + (output_tokens * self.chat_output_price)

    def track(
        self,
        source: str,
        calls: int = 1,
        prompt_tokens: int = 0,
        output_tokens: int = 0,
        cost: Optional[float] = None,
        model: Optional[str] = None,
    ):
        """Records token usage and cost for a given source or model."""
        target_model = model or (self.embed_model if "embed" in source.lower() else self.chat_model)

        if cost is None:
            cost = self.calculate_cost(prompt_tokens, output_tokens, model=target_model)

        if source not in self.records:
            self.records[source] = {
                "calls": 0,
                "prompt_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost": 0.0,
            }

        rec = self.records[source]
        rec["calls"] += calls
        rec["prompt_tokens"] += prompt_tokens
        rec["output_tokens"] += output_tokens
        rec["total_tokens"] += prompt_tokens + output_tokens
        rec["cost"] += cost

    def track_chat(
        self, prompt_tokens: int, output_tokens: int, calls: int = 1, model: Optional[str] = None
    ):
        """Records GraphRAG Direct Chat invocation."""
        target_model = model or self.chat_model
        source = f"GraphRAG Chat ({target_model})"
        self.track(
            source=source,
            calls=calls,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            model=target_model,
        )

    def track_global_search(
        self, prompt_tokens: int, output_tokens: int, calls: int = 1, model: Optional[str] = None
    ):
        """Records GraphRAG Global Search invocation."""
        target_model = model or self.chat_model
        source = f"GraphRAG Global Search ({target_model})"
        self.track(
            source=source,
            calls=calls,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            model=target_model,
        )

    def track_local_search(
        self, prompt_tokens: int, output_tokens: int, calls: int = 1, model: Optional[str] = None
    ):
        """Records GraphRAG Local Search invocation."""
        target_model = model or self.chat_model
        source = f"GraphRAG Local Search ({target_model})"
        self.track(
            source=source,
            calls=calls,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            model=target_model,
        )

    def track_embedding(self, prompt_tokens: int, calls: int = 1, model: Optional[str] = None):
        """Records embedding model invocation."""
        target_model = model or self.embed_model
        source = target_model
        self.track(
            source=source,
            calls=calls,
            prompt_tokens=prompt_tokens,
            output_tokens=0,
            model=target_model,
        )

    def get_totals(self) -> Dict[str, Any]:
        """Calculates total invocations, tokens, and cost across all recorded sources."""
        total_calls = sum(r["calls"] for r in self.records.values())
        total_prompt = sum(r["prompt_tokens"] for r in self.records.values())
        total_output = sum(r["output_tokens"] for r in self.records.values())
        total_tokens = sum(r["total_tokens"] for r in self.records.values())
        total_cost = sum(r["cost"] for r in self.records.values())

        return {
            "total_calls": total_calls,
            "total_prompt_tokens": total_prompt,
            "total_output_tokens": total_output,
            "total_tokens": total_tokens,
            "total_cost": total_cost,
        }

    def format_summary(self) -> str:
        """Formats the ASCII token usage and cost summary table."""
        totals = self.get_totals()

        divider_eq = "=" * 78
        divider_dash = "-" * 78

        lines = [
            "                      LLM TOKEN USAGE & COST SUMMARY",
            divider_eq,
            f" Total LLM Invocations : {totals['total_calls']}",
            f" Total Prompt Tokens   : {totals['total_prompt_tokens']:,}",
            f" Total Output Tokens   : {totals['total_output_tokens']:,}",
            f" Total Tokens Used     : {totals['total_tokens']:,}",
            f" Estimated Total Cost  : ${totals['total_cost']:.4f}",
            divider_dash,
            " Source / Model                   Calls   Prompt     Output     Total      Est. Cost ",
            divider_dash,
        ]

        for source, r in self.records.items():
            if len(source) > 32:
                name_display = source[:29] + "..."
            else:
                name_display = source

            row = (
                f" {name_display:<33}"
                f"{r['calls']:<8,}"
                f"{r['prompt_tokens']:<11,}"
                f"{r['output_tokens']:<11,}"
                f"{r['total_tokens']:<11,}"
                f"${r['cost']:.4f}   "
            )
            lines.append(row)

        return "\n".join(lines)

    def format_markdown_section(self) -> str:
        """Wraps the formatted ASCII table in a markdown section."""
        return f"\n\n### LLM Token Usage & Cost Summary\n\n```\n{self.format_summary()}\n```\n"

    def reset(self):
        """Resets all recorded usage metrics."""
        self.records.clear()

    def to_dict(self) -> Dict[str, Any]:
        """Serializes tracker records and config to a dictionary."""
        return {
            "chat_model": self.chat_model,
            "embed_model": self.embed_model,
            "chat_prompt_price": self.chat_prompt_price,
            "chat_output_price": self.chat_output_price,
            "embed_prompt_price": self.embed_prompt_price,
            "embed_output_price": self.embed_output_price,
            "records": self.records,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TokenCostTracker":
        """Reconstructs a TokenCostTracker from a dictionary."""
        tracker = cls(
            chat_model=data.get("chat_model"),
            embed_model=data.get("embed_model"),
            chat_prompt_price=data.get("chat_prompt_price"),
            chat_output_price=data.get("chat_output_price"),
            embed_prompt_price=data.get("embed_prompt_price"),
        )
        tracker.records = data.get("records", {})
        return tracker

    def save_to_file(self, filepath: str):
        """Saves tracker state to a JSON file."""
        dirname = os.path.dirname(filepath)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load_from_file(cls, filepath: str) -> "TokenCostTracker":
        """Loads tracker state from a JSON file."""
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

    def merge(self, other: "TokenCostTracker"):
        """Merges metrics from another TokenCostTracker instance into this one."""
        if not other or not isinstance(other, TokenCostTracker):
            return
        for source, r in other.records.items():
            if source not in self.records:
                self.records[source] = {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cost": 0.0,
                }
            rec = self.records[source]
            rec["calls"] += r.get("calls", 0)
            rec["prompt_tokens"] += r.get("prompt_tokens", 0)
            rec["output_tokens"] += r.get("output_tokens", 0)
            rec["total_tokens"] += r.get("total_tokens", 0)
            rec["cost"] += r.get("cost", 0.0)

    def enable_litellm_callbacks(self, category: str = "LiteLLM"):
        """Registers a callback with litellm.success_callback to intercept and track
        all direct LiteLLM invocations (e.g. from sdg_hub, custom evaluators).
        """
        if not HAS_LITELLM or litellm is None:
            logging.debug("LiteLLM not available; skipping callback registration.")
            return

        if hasattr(self, "_litellm_callback") and self._litellm_callback is not None:
            if hasattr(litellm, "success_callback") and isinstance(litellm.success_callback, list):
                if self._litellm_callback in litellm.success_callback:
                    return

        def _litellm_success_handler(kwargs, completion_response, start_time, end_time):
            try:
                model = kwargs.get("model") or getattr(completion_response, "model", self.chat_model)
                usage = getattr(completion_response, "usage", None)
                p_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
                o_tokens = getattr(usage, "completion_tokens", 0) if usage else 0

                response_cost = kwargs.get("response_cost")
                if response_cost is None:
                    response_cost = getattr(completion_response, "_response_cost", None)

                source = f"{category} ({model})"
                self.track(
                    source=source,
                    calls=1,
                    prompt_tokens=p_tokens,
                    output_tokens=o_tokens,
                    cost=response_cost,
                    model=model,
                )
            except Exception as e:
                logging.debug(f"Error in litellm success callback: {e}")

        self._litellm_callback = _litellm_success_handler

        if not hasattr(litellm, "success_callback") or not isinstance(litellm.success_callback, list):
            litellm.success_callback = []
        if _litellm_success_handler not in litellm.success_callback:
            litellm.success_callback.append(_litellm_success_handler)

    def disable_litellm_callbacks(self):
        """Unregisters the callback from litellm.success_callback."""
        if not HAS_LITELLM or litellm is None or not hasattr(self, "_litellm_callback"):
            return
        if hasattr(litellm, "success_callback") and isinstance(litellm.success_callback, list):
            if self._litellm_callback in litellm.success_callback:
                litellm.success_callback.remove(self._litellm_callback)
        self._litellm_callback = None

    def log_to_mlflow(self, run_id: Optional[str] = None):
        """Logs aggregated token counts and costs to active MLflow run."""
        try:
            import mlflow
            totals = self.get_totals()
            metrics = {
                "llm_total_calls": totals["total_calls"],
                "llm_total_prompt_tokens": totals["total_prompt_tokens"],
                "llm_total_output_tokens": totals["total_output_tokens"],
                "llm_total_tokens": totals["total_tokens"],
                "llm_total_cost": totals["total_cost"],
            }
            if run_id:
                with mlflow.start_run(run_id=run_id):
                    mlflow.log_metrics(metrics)
            else:
                mlflow.log_metrics(metrics)
        except Exception as e:
            logging.debug(f"MLflow metric logging skipped or failed: {e}")
