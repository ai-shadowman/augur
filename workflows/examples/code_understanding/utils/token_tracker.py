import os
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
                return litellm.token_counter(model=target_model, text=text)
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
