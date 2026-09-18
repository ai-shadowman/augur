from __future__ import annotations

import os
import re
import json
import logging
import tempfile
from typing import Any, Dict, List, Optional, Set, Tuple, Union


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
        git_slug: Optional[str] = None,
        git_repo: Optional[str] = None,
        run_id: Optional[str] = None,
        only_current_run: bool = True,
        print_to_console: Optional[bool] = None,
    ):
        if print_to_console is not None:
            self.print_to_console = print_to_console
        else:
            self.print_to_console = os.getenv("TOKEN_TRACKER_PRINT_CONSOLE", "true").lower() in ("true", "1", "yes")

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
        self.git_slug: Optional[str] = git_slug
        self.git_repo: Optional[str] = git_repo
        self.run_id: Optional[str] = (
            run_id
            or os.environ.get("AUGUR_RUN_ID")
            or os.environ.get("PIPELINE_RUN_ID")
            or os.environ.get("MLFLOW_RUN_ID")
        )
        self.only_current_run: bool = only_current_run
        self._merged_runs: Set[str] = set()
        self._merged_upstream_sources: Set[str] = set()
        self._merged_files: Set[str] = set()

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
        **kwargs,
    ):
        """Records token usage and cost for a given source or model, outputting live metrics to console."""
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
        for k, v in kwargs.items():
            if k not in rec:
                rec[k] = v
        rec["calls"] += calls
        rec["prompt_tokens"] += prompt_tokens
        rec["output_tokens"] += output_tokens
        rec["total_tokens"] += prompt_tokens + output_tokens
        rec["cost"] += cost

        # Real-time console output on every LLM call
        total_tokens = prompt_tokens + output_tokens
        console_msg = (
            f"[LLM Call] Source: {source} | Model: {target_model} | Calls: {calls} | "
            f"Prompt Tokens: {prompt_tokens:,} | Output Tokens: {output_tokens:,} | "
            f"Total Tokens: {total_tokens:,} | Est. Cost: ${cost:.4f}"
        )
        logging.debug(console_msg)
        if getattr(self, "print_to_console", True):
            print(console_msg, flush=True)

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
        source = f"GraphRAG Embeddings ({target_model})"
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

        max_source_len = max([len(s) for s in self.records.keys()] or [0])
        source_col_w = max(33, max_source_len + 2)

        header_str = "Source / Model".ljust(source_col_w)
        header_line = f" {header_str}Calls   Prompt     Output     Total      Est. Cost "
        table_w = max(78, len(header_line.rstrip()))

        divider_eq = "=" * table_w
        divider_dash = "-" * table_w
        title = "LLM TOKEN USAGE & COST SUMMARY"

        lines = [
            f"{title:^{table_w}}",
            divider_eq,
            f" Total LLM Invocations : {totals['total_calls']}",
            f" Total Prompt Tokens   : {totals['total_prompt_tokens']:,}",
            f" Total Output Tokens   : {totals['total_output_tokens']:,}",
            f" Total Tokens Used     : {totals['total_tokens']:,}",
            f" Estimated Total Cost  : ${totals['total_cost']:.4f}",
            divider_dash,
            header_line,
            divider_dash,
        ]

        def get_source_stage_order(src: str) -> int:
            src_lower = src.lower()
            if "data generation" in src_lower:
                return 1
            if "indexing" in src_lower:
                return 2
            return 3

        sorted_records = sorted(
            self.records.items(),
            key=lambda item: get_source_stage_order(item[0]),
        )

        for source, r in sorted_records:
            row = (
                f" {source:<{source_col_w}}"
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
        """Resets all recorded usage metrics and merge history."""
        self.records.clear()
        if hasattr(self, "_merged_runs"):
            self._merged_runs.clear()
        if hasattr(self, "_merged_upstream_sources"):
            self._merged_upstream_sources.clear()
        if hasattr(self, "_merged_files"):
            self._merged_files.clear()

    def to_dict(self) -> Dict[str, Any]:
        """Serializes tracker records and config to a dictionary."""
        d = {
            "chat_model": self.chat_model,
            "embed_model": self.embed_model,
            "chat_prompt_price": self.chat_prompt_price,
            "chat_output_price": self.chat_output_price,
            "embed_prompt_price": self.embed_prompt_price,
            "embed_output_price": self.embed_output_price,
            "records": self.records,
        }
        if self.git_slug:
            d["git_slug"] = self.git_slug
        if self.git_repo:
            d["git_repo"] = self.git_repo
        if getattr(self, "run_id", None):
            d["run_id"] = self.run_id
        if getattr(self, "print_to_console", None) is not None:
            d["print_to_console"] = self.print_to_console
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TokenCostTracker":
        """Reconstructs a TokenCostTracker from a dictionary."""
        tracker = cls(
            chat_model=data.get("chat_model"),
            embed_model=data.get("embed_model"),
            chat_prompt_price=data.get("chat_prompt_price"),
            chat_output_price=data.get("chat_output_price"),
            embed_prompt_price=data.get("embed_prompt_price"),
            print_to_console=data.get("print_to_console"),
        )
        tracker.records = data.get("records", {})
        tracker.git_slug = data.get("git_slug")
        tracker.git_repo = data.get("git_repo")
        tracker.run_id = data.get("run_id")
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

    def merge(self, other: "TokenCostTracker", current_stage: Optional[str] = None):
        """Merges metrics from another TokenCostTracker instance into this one.
        If current_stage is 'Analysis', records belonging to Analysis (e.g. GraphRAG Local Search,
        GraphRAG Chat) are strictly omitted so previous runs do not compound or duplicate."""
        if not other or not isinstance(other, TokenCostTracker):
            return

        # Enforce run isolation if run_id is known on both and current_stage is not provided
        if (
            current_stage is None
            and getattr(self, "only_current_run", True)
            and getattr(other, "only_current_run", True)
            and getattr(self, "run_id", None)
            and getattr(other, "run_id", None)
            and self.run_id != other.run_id
        ):
            logging.warning(
                f"TokenCostTracker: Skipping merge from different run_id '{other.run_id}' "
                f"(current run_id: '{self.run_id}') to maintain current-run isolation."
            )
            return

        if not hasattr(self, "_merged_upstream_sources"):
            self._merged_upstream_sources = set()

        is_analysis = current_stage and current_stage.lower() == "analysis"

        for source, r in other.records.items():
            s_lower = source.lower()
            if is_analysis:
                # If explicitly an indexing or data generation record, always keep it
                if "indexing" in s_lower or "data generation" in s_lower:
                    pass
                else:
                    # Filter out any Analysis-specific records from upstream files/runs
                    if "local search" in s_lower or "graphrag chat" in s_lower or (s_lower.startswith("chat") and "indexing" not in s_lower):
                        continue
                    if r.get("stage", "").lower() == "analysis" or r.get("category", "").lower() == "analysis":
                        continue
                # If an upstream source was already merged, do not add it again
                if source in self._merged_upstream_sources:
                    continue
                self._merged_upstream_sources.add(source)

            if source not in self.records:
                self.records[source] = {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cost": 0.0,
                }
            rec = self.records[source]
            other_calls = r.get("calls", 0)
            other_prompt = r.get("prompt_tokens", 0)
            other_output = r.get("output_tokens", 0)
            # If the exact same record is encountered again (e.g. from copies across candidate dirs), do not double-count
            if rec["calls"] == other_calls and rec["prompt_tokens"] == other_prompt and rec["output_tokens"] == other_output and other_calls > 0:
                continue
            rec["calls"] += other_calls
            rec["prompt_tokens"] += other_prompt
            rec["output_tokens"] += other_output
            rec["total_tokens"] += r.get("total_tokens", 0)
            rec["cost"] += r.get("cost", 0.0)

    def load_and_merge(self, filepath: str, current_stage: Optional[str] = None):
        """Loads token records from a JSON file and merges them into this instance."""
        if not os.path.exists(filepath):
            return
        if not hasattr(self, "_merged_files"):
            self._merged_files = set()
        abs_p = os.path.abspath(filepath)
        if abs_p in self._merged_files:
            return
        try:
            other = TokenCostTracker.load_from_file(filepath)
            self.merge(other, current_stage=current_stage)
            self._merged_files.add(abs_p)
        except Exception as e:
            logging.debug(f"Failed to load and merge tokens from {filepath}: {e}")

    def set_category(self, category: str):
        """Sets the active category used by callback interceptors for upcoming calls."""
        self._active_category = category

    def enable_litellm_callbacks(self, category: str = "LiteLLM"):
        """Registers a callback with litellm.success_callback to intercept and track
        all direct LiteLLM invocations (e.g. from sdg_hub, custom evaluators).
        """
        self._active_category = category
        if not HAS_LITELLM or litellm is None:
            logging.debug("LiteLLM not available; skipping callback registration.")
            return

        tracker_self = self

        def _litellm_success_handler(kwargs, completion_response, start_time, end_time):
            try:
                cat = getattr(tracker_self, "_active_category", None) or category
                model = kwargs.get("model") or getattr(completion_response, "model", tracker_self.chat_model)
                usage = getattr(completion_response, "usage", None)
                p_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
                o_tokens = getattr(usage, "completion_tokens", 0) if usage else 0

                response_cost = kwargs.get("response_cost")
                if response_cost is None:
                    response_cost = getattr(completion_response, "_response_cost", None)

                call_type = kwargs.get("call_type", "")
                is_embed = "embed" in str(call_type).lower() or "embed" in str(model).lower() or (o_tokens == 0 and p_tokens > 0 and (model == tracker_self.embed_model or "embed" in tracker_self.embed_model))
                if is_embed:
                    prefix = cat if "Embeddings" in cat else f"{cat} Embeddings"
                    source = f"{prefix} ({model})"
                else:
                    source = f"{cat} ({model})"
                tracker_self.track(
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

        if hasattr(litellm, "_async_success_callback") and isinstance(litellm._async_success_callback, list):
            if _litellm_success_handler not in litellm._async_success_callback:
                litellm._async_success_callback.append(_litellm_success_handler)

        if hasattr(litellm, "callbacks") and isinstance(litellm.callbacks, list):
            if _litellm_success_handler not in litellm.callbacks:
                litellm.callbacks.append(_litellm_success_handler)

    def disable_litellm_callbacks(self):
        """Unregisters the callback from litellm.success_callback."""
        if not HAS_LITELLM or litellm is None or not hasattr(self, "_litellm_callback"):
            return
        cb = self._litellm_callback
        for cb_list_name in ["success_callback", "_async_success_callback", "callbacks"]:
            if hasattr(litellm, cb_list_name) and isinstance(getattr(litellm, cb_list_name), list):
                cb_list = getattr(litellm, cb_list_name)
                if cb in cb_list:
                    cb_list.remove(cb)
        self._litellm_callback = None

    def enable_openai_tracking(self, category: str = "GraphRAG Indexing"):
        """Intercepts OpenAI chat completions and embeddings calls when direct OpenAI client
        is used (e.g. by GraphRAG indexing) so token usage is captured."""
        self._active_category = category
        try:
            import openai
        except ImportError:
            logging.debug("OpenAI package not available; skipping direct OpenAI tracking.")
            return

        if getattr(self, "_openai_tracking_enabled", False):
            return

        try:
            from openai.resources.chat import completions as chat_mod
            from openai.resources import embeddings as embed_mod
        except Exception as e:
            logging.debug(f"Unable to access openai resource modules: {e}")
            return

        tracker_self = self

        # 1. Patch AsyncCompletions.create
        if hasattr(chat_mod, "AsyncCompletions") and hasattr(chat_mod.AsyncCompletions, "create"):
            self._orig_async_chat = chat_mod.AsyncCompletions.create

            async def wrapped_async_chat(*args, **kwargs):
                resp = await tracker_self._orig_async_chat(*args, **kwargs)
                try:
                    cat = getattr(tracker_self, "_active_category", None) or category
                    model = kwargs.get("model") or getattr(resp, "model", tracker_self.chat_model)
                    usage = getattr(resp, "usage", None)
                    p_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
                    o_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
                    source = f"{cat} ({model})"
                    tracker_self.track(
                        source=source,
                        calls=1,
                        prompt_tokens=p_tokens,
                        output_tokens=o_tokens,
                        model=model,
                    )
                except Exception as e:
                    logging.debug(f"Error in openai tracking callback: {e}")
                return resp

            chat_mod.AsyncCompletions.create = wrapped_async_chat

        # 2. Patch Completions.create (sync)
        if hasattr(chat_mod, "Completions") and hasattr(chat_mod.Completions, "create"):
            self._orig_sync_chat = chat_mod.Completions.create

            def wrapped_sync_chat(*args, **kwargs):
                resp = tracker_self._orig_sync_chat(*args, **kwargs)
                try:
                    cat = getattr(tracker_self, "_active_category", None) or category
                    model = kwargs.get("model") or getattr(resp, "model", tracker_self.chat_model)
                    usage = getattr(resp, "usage", None)
                    p_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
                    o_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
                    source = f"{cat} ({model})"
                    tracker_self.track(
                        source=source,
                        calls=1,
                        prompt_tokens=p_tokens,
                        output_tokens=o_tokens,
                        model=model,
                    )
                except Exception as e:
                    logging.debug(f"Error in openai sync tracking callback: {e}")
                return resp

            chat_mod.Completions.create = wrapped_sync_chat

        # 3. Patch AsyncEmbeddings.create
        if hasattr(embed_mod, "AsyncEmbeddings") and hasattr(embed_mod.AsyncEmbeddings, "create"):
            self._orig_async_embed = embed_mod.AsyncEmbeddings.create

            async def wrapped_async_embed(*args, **kwargs):
                resp = await tracker_self._orig_async_embed(*args, **kwargs)
                try:
                    cat = getattr(tracker_self, "_active_category", None) or category
                    model = kwargs.get("model") or getattr(resp, "model", tracker_self.embed_model)
                    usage = getattr(resp, "usage", None)
                    p_tokens = getattr(usage, "prompt_tokens", 0) if usage else (getattr(usage, "total_tokens", 0) if usage else 0)
                    prefix = cat if "Embeddings" in cat else f"{cat} Embeddings"
                    source = f"{prefix} ({model})"
                    tracker_self.track(
                        source=source,
                        calls=1,
                        prompt_tokens=p_tokens,
                        output_tokens=0,
                        model=model,
                    )
                except Exception as e:
                    logging.debug(f"Error in openai embed tracking callback: {e}")
                return resp

            embed_mod.AsyncEmbeddings.create = wrapped_async_embed

        # 4. Patch Embeddings.create (sync)
        if hasattr(embed_mod, "Embeddings") and hasattr(embed_mod.Embeddings, "create"):
            self._orig_sync_embed = embed_mod.Embeddings.create

            def wrapped_sync_embed(*args, **kwargs):
                resp = tracker_self._orig_sync_embed(*args, **kwargs)
                try:
                    cat = getattr(tracker_self, "_active_category", None) or category
                    model = kwargs.get("model") or getattr(resp, "model", tracker_self.embed_model)
                    usage = getattr(resp, "usage", None)
                    p_tokens = getattr(usage, "prompt_tokens", 0) if usage else (getattr(usage, "total_tokens", 0) if usage else 0)
                    prefix = cat if "Embeddings" in cat else f"{cat} Embeddings"
                    source = f"{prefix} ({model})"
                    tracker_self.track(
                        source=source,
                        calls=1,
                        prompt_tokens=p_tokens,
                        output_tokens=0,
                        model=model,
                    )
                except Exception as e:
                    logging.debug(f"Error in openai sync embed tracking callback: {e}")
                return resp

            embed_mod.Embeddings.create = wrapped_sync_embed

        self._openai_tracking_enabled = True
        logging.debug(f"TokenCostTracker: OpenAI tracking enabled for category '{category}'")

    def disable_openai_tracking(self):
        """Restores original unpatched OpenAI methods if patched."""
        if not getattr(self, "_openai_tracking_enabled", False):
            return
        try:
            from openai.resources.chat import completions as chat_mod
            from openai.resources import embeddings as embed_mod

            if hasattr(self, "_orig_async_chat"):
                chat_mod.AsyncCompletions.create = self._orig_async_chat
            if hasattr(self, "_orig_sync_chat"):
                chat_mod.Completions.create = self._orig_sync_chat
            if hasattr(self, "_orig_async_embed"):
                embed_mod.AsyncEmbeddings.create = self._orig_async_embed
            if hasattr(self, "_orig_sync_embed"):
                embed_mod.Embeddings.create = self._orig_sync_embed
        except Exception:
            pass
        self._openai_tracking_enabled = False

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
            for source, r in self.records.items():
                source_clean = re.sub(r"[^a-zA-Z0-9_]", "_", source.lower()).strip("_")[:200]
                metrics[f"llm_{source_clean}_calls"] = r.get("calls", 0)
                metrics[f"llm_{source_clean}_tokens"] = r.get("total_tokens", 0)
                metrics[f"llm_{source_clean}_cost"] = r.get("cost", 0.0)

            active_run = mlflow.active_run()
            if run_id:
                if active_run and active_run.info.run_id == run_id:
                    mlflow.log_metrics(metrics)
                else:
                    with mlflow.start_run(run_id=run_id, nested=bool(active_run)):
                        mlflow.log_metrics(metrics)
            else:
                if active_run:
                    mlflow.log_metrics(metrics)
                    mlflow.end_run()
                else:
                    with mlflow.start_run():
                        mlflow.log_metrics(metrics)
        except Exception as e:
            logging.debug(f"MLflow metric logging skipped or failed: {e}")

    def upload_to_mlflow(
        self,
        git_slug: Optional[str] = None,
        stage: Optional[str] = None,
        run_id: Optional[str] = None,
        multi_repo: bool = False,
    ):
        """Uploads token metrics and tokens.json artifact to MLflow."""
        if not self.records:
            return

        # 1. Log numerical metrics
        try:
            self.log_to_mlflow(run_id=run_id)
        except Exception as e:
            logging.debug(f"Failed to log token metrics to MLflow: {e}")

        # 2. Upload tokens.json artifact
        temp_dir = tempfile.mkdtemp()
        temp_file = os.path.join(temp_dir, "tokens.json")
        try:
            self.save_to_file(temp_file)

            # Direct MLflow run upload if active run or run_id available
            try:
                import mlflow
                active_run = mlflow.active_run()
                target_run = run_id or (active_run.info.run_id if active_run else None) or os.environ.get("MLFLOW_RUN_ID")
                if target_run:
                    run_tags = {
                        "category": "telemetry",
                        "type": "tokens",
                        "git_slug": str(git_slug or "multi-repo"),
                    }
                    if stage:
                        run_tags["stage"] = str(stage)
                    if active_run and active_run.info.run_id == target_run:
                        mlflow.set_tags(run_tags)
                        mlflow.log_artifact(temp_file, artifact_path="telemetry")
                    else:
                        with mlflow.start_run(run_id=target_run, nested=bool(active_run)):
                            mlflow.set_tags(run_tags)
                            mlflow.log_artifact(temp_file, artifact_path="telemetry")
            except Exception as e:
                logging.debug(f"Failed to log tokens.json directly to MLflow run: {e}")

            # Catalog upload via DefaultAssetLoader for git_slug / tag search
            if git_slug or multi_repo:
                try:
                    from loaders.default_asset_loader import DefaultAssetLoader
                    artifact_path = DefaultAssetLoader.get_log_results_artifact_path(
                        DefaultAssetLoader.RESULTS_PATH_PREFIX_TELEMETRY,
                        git_slug=git_slug,
                        multi_repo=multi_repo,
                    )
                    tags = {
                        "git_slug": str(git_slug or "multi-repo"),
                        "category": "telemetry",
                        "type": "tokens",
                        "multi_repo": str(multi_repo),
                    }
                    if stage:
                        tags["stage"] = str(stage)

                    content_str = None
                    try:
                        with open(temp_file, "r", encoding="utf-8") as f:
                            content_str = f.read()
                    except Exception:
                        pass

                    DefaultAssetLoader().log_results(
                        temp_file,
                        artifact_path=artifact_path,
                        tags=tags,
                        content=content_str,
                    )
                except Exception as e:
                    logging.debug(f"Failed to upload tokens.json via DefaultAssetLoader: {e}")
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def download_from_mlflow(
        self,
        git_slug: Optional[str] = None,
        run_id: Optional[str] = None,
        multi_repo: bool = False,
        current_stage: Optional[str] = None,
        only_current_run: bool = True,
    ) -> bool:
        """Downloads and merges token usage records from MLflow.
        If only_current_run is True (default) and no specific run_id or MLFLOW_RUN_ID is given,
        cross-run searching in MLflow is bypassed to prevent metrics from other runs being merged.
        If current_stage is specified, runs tagged with that stage are skipped,
        and only upstream stages (e.g. Data Generation, Indexing for Analysis) are accepted.
        Returns True if records were retrieved and merged, False otherwise."""
        merged_any = False
        if not hasattr(self, "_merged_runs"):
            self._merged_runs = set()

        # 1. Try downloading via run_id or MLFLOW_RUN_ID
        target_run = run_id or os.environ.get("MLFLOW_RUN_ID")
        if target_run and target_run not in self._merged_runs:
            try:
                import mlflow
                local_path = mlflow.artifacts.download_artifacts(
                    run_id=target_run, artifact_path="telemetry/tokens.json"
                )
                if local_path:
                    target_file = None
                    if os.path.isfile(local_path):
                        target_file = local_path
                    elif os.path.isdir(local_path):
                        cand = os.path.join(local_path, "tokens.json")
                        if os.path.isfile(cand):
                            target_file = cand
                    if target_file and os.path.exists(target_file):
                        self.load_and_merge(target_file, current_stage=current_stage)
                        self._merged_runs.add(target_run)
                        merged_any = True
            except Exception as e:
                logging.debug(f"Failed to download tokens.json for run {target_run}: {e}")

        # If tracking only the current run, no specific run_id was provided, and no stage
        # transition is active, skip searching across historical runs in MLflow.
        should_isolate_current_run = only_current_run and getattr(self, "only_current_run", True) and (current_stage is None)
        if should_isolate_current_run and not target_run:
            logging.info(
                "TokenCostTracker: only_current_run is True, no current_stage specified, and no specific run_id provided. "
                "Skipping cross-run search in MLflow to ensure tokens are strictly for the current run."
            )
            return merged_any

        # 2. Try searching and aggregating across ALL telemetry runs in MLflow
        seen_stages = set()
        try:
            import mlflow
            from mlflow.tracking import MlflowClient
            from loaders.mlflow_asset_loader import MlFlowAssetLoader

            client = MlflowClient()

            experiment_ids = []
            try:
                active_run = mlflow.active_run()
                if active_run and hasattr(active_run, "info") and getattr(active_run.info, "experiment_id", None):
                    experiment_ids.append(str(active_run.info.experiment_id))
            except Exception:
                pass

            exp_name = os.environ.get("MLFLOW_EXPERIMENT_NAME")
            if exp_name:
                try:
                    exp = client.get_experiment_by_name(exp_name)
                    if exp and exp.experiment_id:
                        experiment_ids.append(str(exp.experiment_id))
                except Exception:
                    pass

            if getattr(self, "mlflow_experiment_id", None):
                experiment_ids.append(str(self.mlflow_experiment_id))

            try:
                result_exp = MlFlowAssetLoader().get_or_create_experiment_by_name(
                    client, MlFlowAssetLoader.RESULT_ASSET_EXPERIMENT
                )
                if result_exp and result_exp.experiment_id:
                    experiment_ids.append(str(result_exp.experiment_id))
            except Exception:
                pass

            experiment_ids.append("0")
            unique_exp_ids = [eid for i, eid in enumerate(experiment_ids) if eid and eid not in experiment_ids[:i]]

            filter_parts = ["tags.category = 'telemetry'", "tags.type = 'tokens'"]
            if git_slug:
                filter_parts.append(f"tags.git_slug = '{git_slug}'")
            elif multi_repo:
                filter_parts.append("tags.git_slug = 'multi-repo'")
            filter_string = " AND ".join(filter_parts)

            runs = []
            try:
                runs = client.search_runs(
                    experiment_ids=unique_exp_ids,
                    filter_string=filter_string,
                    order_by=["attributes.start_time DESC"],
                )
            except Exception:
                for eid in unique_exp_ids:
                    try:
                        found_runs = client.search_runs(
                            experiment_ids=[eid],
                            filter_string=filter_string,
                            order_by=["attributes.start_time DESC"],
                        )
                        if found_runs:
                            runs.extend(found_runs)
                    except Exception:
                        pass
                if not runs:
                    try:
                        quoted_parts = [
                            f'tags."{p.split(" = ")[0].split(".", 1)[1]}" = {p.split(" = ")[1]}'
                            for p in filter_parts
                        ]
                        quoted_filter = " AND ".join(quoted_parts)
                        for eid in unique_exp_ids:
                            try:
                                found_runs = client.search_runs(
                                    experiment_ids=[eid],
                                    filter_string=quoted_filter,
                                    order_by=["attributes.start_time DESC"],
                                )
                                if found_runs:
                                    runs.extend(found_runs)
                            except Exception:
                                pass
                    except Exception:
                        pass

            allowed_stages = None
            if current_stage and current_stage.lower() == "analysis":
                allowed_stages = {"data generation", "indexing"}
            elif current_stage and current_stage.lower() == "indexing":
                allowed_stages = {"data generation"}

            for run in runs:
                if run.info.run_id in self._merged_runs:
                    continue
                stage = run.data.tags.get("stage")
                if current_stage and stage and stage.lower() == current_stage.lower():
                    continue
                if allowed_stages is not None:
                    if not stage or stage.lower() not in allowed_stages:
                        continue
                elif current_stage and not stage:
                    continue

                if stage:
                    if stage.lower() in seen_stages:
                        continue
                else:
                    if "untagged" in seen_stages:
                        continue

                candidate_subpaths = []
                if git_slug:
                    candidate_subpaths.append(f"results/telemetry/{git_slug}/tokens.json")
                    candidate_subpaths.append(f"results/telemetry/{git_slug}")
                if multi_repo:
                    candidate_subpaths.append(f"results/telemetry/multi-repo/{git_slug or ''}/tokens.json".replace("//", "/"))
                    candidate_subpaths.append("results/telemetry/multi-repo/tokens.json")
                candidate_subpaths.extend([
                    "results/telemetry/tokens.json",
                    "telemetry/tokens.json",
                    "tokens.json",
                ])

                run_merged = False
                for subpath in candidate_subpaths:
                    try:
                        downloaded_path = mlflow.artifacts.download_artifacts(
                            run_id=run.info.run_id,
                            artifact_path=subpath,
                        )
                        if downloaded_path:
                            target_file = None
                            if os.path.isfile(downloaded_path):
                                target_file = downloaded_path
                            elif os.path.isdir(downloaded_path):
                                cand = os.path.join(downloaded_path, "tokens.json")
                                if os.path.isfile(cand):
                                    target_file = cand
                            if target_file and os.path.exists(target_file):
                                self.load_and_merge(target_file, current_stage=current_stage)
                                self._merged_runs.add(run.info.run_id)
                                merged_any = True
                                run_merged = True
                                if stage:
                                    seen_stages.add(stage.lower())
                                else:
                                    seen_stages.add("untagged")
                                break
                    except Exception:
                        continue
                if run_merged and allowed_stages and seen_stages.issuperset(allowed_stages):
                    break

        except Exception as e:
            logging.debug(f"MLflow client multi-run search for tokens.json failed: {e}")

        # Fallback to DefaultAssetLoader for missing upstream stages
        upstream_targets = []
        if current_stage and current_stage.lower() == "analysis":
            upstream_targets = ["Data Generation", "Indexing"]
        elif current_stage and current_stage.lower() == "indexing":
            upstream_targets = ["Data Generation"]
        else:
            upstream_targets = [None]

        for target_stage in upstream_targets:
            if target_stage and target_stage.lower() in seen_stages:
                continue
            if not (git_slug or multi_repo):
                continue
            try:
                from loaders.default_asset_loader import DefaultAssetLoader
                from loaders.mlflow_asset_loader import MlFlowAssetLoader

                artifact_path = DefaultAssetLoader.get_log_results_artifact_path(
                    DefaultAssetLoader.RESULTS_PATH_PREFIX_TELEMETRY,
                    git_slug=git_slug,
                    multi_repo=multi_repo,
                )
                asset_file = f"{artifact_path}/tokens.json"
                temp_dir = tempfile.mkdtemp()
                try:
                    asset_tags = {"git_slug": str(git_slug or "multi-repo"), "type": "tokens"}
                    if target_stage:
                        asset_tags["stage"] = target_stage
                    content = DefaultAssetLoader().download(
                        asset_file,
                        download_dir=temp_dir,
                        experiment_name=MlFlowAssetLoader.RESULT_ASSET_EXPERIMENT,
                        asset_tags=asset_tags,
                    )
                    loaded_from_content = False
                    if isinstance(content, dict) and "records" in content:
                        other = TokenCostTracker.from_dict(content)
                        self.merge(other, current_stage=current_stage)
                        merged_any = True
                        loaded_from_content = True
                    elif isinstance(content, str):
                        data = json.loads(content)
                        if isinstance(data, dict) and "records" in data:
                            other = TokenCostTracker.from_dict(data)
                            self.merge(other, current_stage=current_stage)
                            merged_any = True
                            loaded_from_content = True

                    if not loaded_from_content:
                        downloaded_file = os.path.join(temp_dir, "tokens.json")
                        if os.path.exists(downloaded_file):
                            self.load_and_merge(downloaded_file, current_stage=current_stage)
                            merged_any = True
                    if target_stage:
                        seen_stages.add(target_stage.lower())
                finally:
                    import shutil
                    shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception as e:
                logging.debug(f"Failed to download tokens.json for {target_stage} via DefaultAssetLoader: {e}")

        return merged_any


def extract_graphrag_indexing_tokens(
    graphrag_dir: str,
    token_tracker: Optional["TokenCostTracker"] = None,
) -> bool:
    """Extracts GraphRAG indexing token usage from output files (stats.json, text_units.parquet, etc.)
    and records them into the TokenCostTracker instance.

    Args:
        graphrag_dir: Root directory of the GraphRAG project or its output folder.
        token_tracker: TokenCostTracker instance to update. If None, uses TokenCostTracker.get_instance().

    Returns:
        bool: True if any indexing tokens or calls were discovered and tracked, False otherwise.
    """
    if not graphrag_dir:
        return False

    graphrag_dir = str(graphrag_dir)
    if token_tracker is None:
        token_tracker = TokenCostTracker.get_instance()

    extracted_any = False
    chat_model = token_tracker.chat_model
    embed_model = token_tracker.embed_model

    # 1. Search for stats.json across common output locations
    candidate_stats_files = [
        os.path.join(graphrag_dir, "output", "stats.json"),
        os.path.join(graphrag_dir, "output", "artifacts", "stats.json"),
        os.path.join(graphrag_dir, "logs", "stats.json"),
        os.path.join(graphrag_dir, "stats.json"),
    ]
    for search_subdir in ["output", "logs"]:
        base_sub = os.path.join(graphrag_dir, search_subdir)
        if os.path.isdir(base_sub):
            try:
                for root, _, files in os.walk(base_sub):
                    if "stats.json" in files:
                        p = os.path.join(root, "stats.json")
                        if p not in candidate_stats_files:
                            candidate_stats_files.append(p)
            except Exception:
                pass

    stats_file = next((f for f in candidate_stats_files if os.path.isfile(f)), None)

    total_chat_calls = 0
    total_chat_prompt = 0
    total_chat_output = 0
    total_embed_calls = 0
    total_embed_prompt = 0

    if stats_file:
        try:
            with open(stats_file, "r", encoding="utf-8") as f:
                stats_data = json.load(f)

            if isinstance(stats_data, dict):
                # Format A: GraphRAG workflow dictionary
                workflows = stats_data.get("workflows")
                if isinstance(workflows, dict):
                    for wf_name, wf_info in workflows.items():
                        if not isinstance(wf_info, dict):
                            continue
                        calls = wf_info.get("llm_calls", 0)
                        prompt = wf_info.get("prompt_tokens", 0)
                        output = wf_info.get("completion_tokens", wf_info.get("output_tokens", 0))

                        is_embed_wf = "embed" in wf_name.lower()
                        if is_embed_wf:
                            total_embed_calls += calls
                            total_embed_prompt += prompt
                        else:
                            total_chat_calls += calls
                            total_chat_prompt += prompt
                            total_chat_output += output

                # Format B: Flat / top-level keys
                if "llm_calls" in stats_data or "prompt_tokens" in stats_data:
                    calls = stats_data.get("llm_calls", 0)
                    prompt = stats_data.get("prompt_tokens", 0)
                    output = stats_data.get("completion_tokens", stats_data.get("output_tokens", 0))
                    total_chat_calls = max(total_chat_calls, calls)
                    total_chat_prompt = max(total_chat_prompt, prompt)
                    total_chat_output = max(total_chat_output, output)

        except Exception as e:
            logging.debug(f"Failed to parse stats.json at {stats_file}: {e}")

    # Track chat tokens if found in stats.json and not yet tracked
    if total_chat_calls > 0 or total_chat_prompt > 0 or total_chat_output > 0:
        existing_chat_prompt = sum(
            r.get("prompt_tokens", 0)
            for k, r in token_tracker.records.items()
            if "Indexing" in k and "Embeddings" not in k
        )
        if existing_chat_prompt == 0:
            token_tracker.track(
                source=f"GraphRAG Indexing ({chat_model})",
                calls=max(1, total_chat_calls),
                prompt_tokens=total_chat_prompt,
                output_tokens=total_chat_output,
                model=chat_model,
            )
            extracted_any = True

    # 2. Search for text_units.parquet for embedding tokens
    candidate_tu_files = [
        os.path.join(graphrag_dir, "output", "text_units.parquet"),
        os.path.join(graphrag_dir, "output", "artifacts", "text_units.parquet"),
        os.path.join(graphrag_dir, "text_units.parquet"),
    ]
    base_output = os.path.join(graphrag_dir, "output")
    if os.path.isdir(base_output):
        try:
            for root, _, files in os.walk(base_output):
                if "text_units.parquet" in files:
                    p = os.path.join(root, "text_units.parquet")
                    if p not in candidate_tu_files:
                        candidate_tu_files.append(p)
        except Exception:
            pass

    tu_file = next((f for f in candidate_tu_files if os.path.isfile(f)), None)

    existing_embed_prompt = sum(
        r.get("prompt_tokens", 0)
        for k, r in token_tracker.records.items()
        if "Indexing Embeddings" in k
    )

    if tu_file and existing_embed_prompt == 0:
        try:
            import pandas as pd
            tu_df = pd.read_parquet(tu_file)
            calls = len(tu_df)
            if "n_tokens" in tu_df.columns:
                p_tokens = int(tu_df["n_tokens"].sum())
            elif "text" in tu_df.columns:
                p_tokens = sum(max(1, len(str(t).split()) * 4 // 3) for t in tu_df["text"])
            else:
                p_tokens = calls * 300

            if calls > 0:
                token_tracker.track(
                    source=f"GraphRAG Indexing Embeddings ({embed_model})",
                    calls=calls,
                    prompt_tokens=p_tokens,
                    output_tokens=0,
                    model=embed_model,
                )
                extracted_any = True
        except Exception as e:
            logging.debug(f"Failed to read text_units.parquet via pandas: {e}")
            try:
                import pyarrow.parquet as pq
                table = pq.read_table(tu_file)
                calls = table.num_rows
                p_tokens = calls * 300
                if "n_tokens" in table.column_names:
                    p_tokens = sum(table["n_tokens"].to_pylist())
                if calls > 0:
                    token_tracker.track(
                        source=f"GraphRAG Indexing Embeddings ({embed_model})",
                        calls=calls,
                        prompt_tokens=p_tokens,
                        output_tokens=0,
                        model=embed_model,
                    )
                    extracted_any = True
            except Exception as e2:
                logging.debug(f"Failed to read text_units.parquet via pyarrow: {e2}")

    # If stats.json had embed tokens and parquet wasn't read:
    if total_embed_prompt > 0:
        existing_embed = any("Indexing Embeddings" in k for k in token_tracker.records)
        if not existing_embed:
            token_tracker.track(
                source=f"GraphRAG Indexing Embeddings ({embed_model})",
                calls=max(1, total_embed_calls),
                prompt_tokens=total_embed_prompt,
                output_tokens=0,
                model=embed_model,
            )
            extracted_any = True

    # 3. Fallback: community_reports.parquet / entities.parquet if chat tokens still absent
    existing_chat = any("Indexing" in k and "Embeddings" not in k for k in token_tracker.records)
    if not existing_chat:
        candidate_cr_files = [
            os.path.join(graphrag_dir, "output", "community_reports.parquet"),
            os.path.join(graphrag_dir, "output", "artifacts", "community_reports.parquet"),
        ]
        cr_file = next((f for f in candidate_cr_files if os.path.isfile(f)), None)
        if cr_file:
            try:
                import pandas as pd
                cr_df = pd.read_parquet(cr_file)
                n_reports = len(cr_df)
                if n_reports > 0:
                    token_tracker.track(
                        source=f"GraphRAG Indexing ({chat_model})",
                        calls=n_reports,
                        prompt_tokens=n_reports * 2000,
                        output_tokens=n_reports * 400,
                        model=chat_model,
                    )
                    extracted_any = True
            except Exception:
                pass

    return extracted_any


def extract_data_generation_tokens(search_paths: Union[str, List[str]], token_tracker: TokenCostTracker) -> bool:
    """Extracts/reconstructs Data Generation token usage from generated code and metadata files if not already tracked.
    Returns True if any Data Generation tokens were extracted/recorded, False otherwise."""
    if not search_paths or not token_tracker:
        return False

    # If tracker already contains Data Generation records, do nothing
    has_data_gen = any("Data Generation" in k for k in token_tracker.records)
    if has_data_gen:
        return False

    if isinstance(search_paths, str):
        search_paths = [search_paths]

    model_name = os.getenv("METADATA_LLM_ID") or os.getenv("GRAPHRAG_LLM_ID") or "gpt-oss-120b"

    # Find metadata files (*_metadata.txt) or parsed code files
    metadata_files = []
    for sp in search_paths:
        if not sp or not os.path.exists(sp):
            continue
        if os.path.isfile(sp):
            if sp.endswith("_metadata.txt") or sp.endswith("code-metadata.json") or sp.endswith("metadata.json"):
                metadata_files.append(sp)
        elif os.path.isdir(sp):
            for root, _, files in os.walk(sp):
                for f in files:
                    if f.endswith("_metadata.txt") or f in ("code-metadata.json", "metadata.json"):
                        metadata_files.append(os.path.join(root, f))

    if not metadata_files:
        # Check for input code files (.txt) if no _metadata.txt
        txt_files = []
        for sp in search_paths:
            if not sp or not os.path.exists(sp):
                continue
            cand_dirs = [sp, os.path.join(sp, "input")] if os.path.isdir(sp) else [sp]
            for cd in cand_dirs:
                if os.path.isdir(cd):
                    for root, _, files in os.walk(cd):
                        for f in files:
                            if f.endswith(".txt") and not f.endswith("_metadata.txt") and not f.startswith("."):
                                txt_files.append(os.path.join(root, f))
        if not txt_files:
            return False

        calls = len(txt_files)
        total_chars = 0
        lang_counts = {}
        for tf in txt_files:
            try:
                size = os.path.getsize(tf)
                total_chars += size
                parts = os.path.basename(tf).split(".")
                lang = parts[-2] if len(parts) >= 3 else "code"
                lang_counts[lang] = lang_counts.get(lang, 0) + 1
            except Exception:
                pass

        prompt_tokens = max(calls * 300, total_chars // 4 + calls * 150)
        output_tokens = max(calls * 150, calls * 100)
        for lang, count in lang_counts.items():
            frac = count / calls
            source_key = f"Data Generation ({lang}) ({model_name})"
            token_tracker.track(
                source=source_key,
                calls=count,
                prompt_tokens=int(prompt_tokens * frac),
                output_tokens=int(output_tokens * frac),
                model=model_name,
                stage="Data Generation",
                category="Data Generation",
            )
        return True

    # Group by language
    per_lang_stats = {}
    for mf in metadata_files:
        if mf.endswith(".json"):
            try:
                with open(mf, "r", encoding="utf-8") as jf:
                    jdata = json.load(jf)
                items = jdata if isinstance(jdata, list) else [jdata]
                for item in items:
                    lang = item.get("language", "code") if isinstance(item, dict) else "code"
                    if lang not in per_lang_stats:
                        per_lang_stats[lang] = {"calls": 0, "prompt_chars": 0, "output_chars": 0}
                    per_lang_stats[lang]["calls"] += 1
                    per_lang_stats[lang]["prompt_chars"] += len(str(item.get("code", ""))) or 1000
                    per_lang_stats[lang]["output_chars"] += len(str(item.get("metadata", ""))) or 500
            except Exception:
                pass
            continue

        base_code_path = mf[:-len("_metadata.txt")] + ".txt"
        fname = os.path.basename(mf)
        parts = fname.replace("_metadata.txt", "").split(".")
        lang = parts[-1] if len(parts) >= 2 else "code"
        if lang not in per_lang_stats:
            per_lang_stats[lang] = {"calls": 0, "prompt_chars": 0, "output_chars": 0}
        per_lang_stats[lang]["calls"] += 1

        output_size = 500
        try:
            output_size = os.path.getsize(mf)
        except Exception:
            pass
        per_lang_stats[lang]["output_chars"] += output_size

        prompt_size = 1200
        if os.path.exists(base_code_path):
            try:
                prompt_size = os.path.getsize(base_code_path)
            except Exception:
                pass
        per_lang_stats[lang]["prompt_chars"] += prompt_size

    extracted_any = False
    for lang, stats in per_lang_stats.items():
        if stats["calls"] <= 0:
            continue
        p_tokens = max(stats["calls"] * 200, stats["prompt_chars"] // 4 + stats["calls"] * 150)
        o_tokens = max(stats["calls"] * 100, stats["output_chars"] // 4)
        source_key = f"Data Generation ({lang}) ({model_name})"
        token_tracker.track(
            source=source_key,
            calls=stats["calls"],
            prompt_tokens=p_tokens,
            output_tokens=o_tokens,
            model=model_name,
            stage="Data Generation",
            category="Data Generation",
        )
        extracted_any = True

    return extracted_any



