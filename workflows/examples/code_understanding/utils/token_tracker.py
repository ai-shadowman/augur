import os
import re
import json
import logging
import tempfile
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

        # Enforce run isolation if run_id is known on both
        if (
            getattr(self, "only_current_run", True)
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
                # Strictly filter out any Analysis-specific records from upstream files/runs
                if "local search" in s_lower or "chat" in s_lower:
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
            rec["calls"] += r.get("calls", 0)
            rec["prompt_tokens"] += r.get("prompt_tokens", 0)
            rec["output_tokens"] += r.get("output_tokens", 0)
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

                call_type = kwargs.get("call_type", "")
                is_embed = "embed" in str(call_type).lower() or "embed" in str(model).lower() or (o_tokens == 0 and p_tokens > 0 and (model == self.embed_model or "embed" in self.embed_model))
                if is_embed:
                    prefix = category if "Embeddings" in category else f"{category} Embeddings"
                    source = f"{prefix} ({model})"
                else:
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

    def enable_openai_tracking(self, category: str = "GraphRAG Indexing"):
        """Intercepts OpenAI chat completions and embeddings calls when direct OpenAI client
        is used (e.g. by GraphRAG indexing) so token usage is captured."""
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
                    model = kwargs.get("model") or getattr(resp, "model", tracker_self.chat_model)
                    usage = getattr(resp, "usage", None)
                    p_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
                    o_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
                    source = f"{category} ({model})"
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
                    model = kwargs.get("model") or getattr(resp, "model", tracker_self.chat_model)
                    usage = getattr(resp, "usage", None)
                    p_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
                    o_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
                    source = f"{category} ({model})"
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
                    model = kwargs.get("model") or getattr(resp, "model", tracker_self.embed_model)
                    usage = getattr(resp, "usage", None)
                    p_tokens = getattr(usage, "prompt_tokens", 0) if usage else (getattr(usage, "total_tokens", 0) if usage else 0)
                    source = f"{category} Embeddings ({model})"
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
                    model = kwargs.get("model") or getattr(resp, "model", tracker_self.embed_model)
                    usage = getattr(resp, "usage", None)
                    p_tokens = getattr(usage, "prompt_tokens", 0) if usage else (getattr(usage, "total_tokens", 0) if usage else 0)
                    source = f"{category} Embeddings ({model})"
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

        # If tracking only the current run and no specific run_id was provided,
        # skip searching across historical runs in MLflow to ensure telemetry reflects
        # only the current run and does not take into account other runs.
        should_isolate_current_run = only_current_run and getattr(self, "only_current_run", True)
        if should_isolate_current_run and not target_run:
            logging.info(
                "TokenCostTracker: only_current_run is True and no specific run_id provided. "
                "Skipping cross-run search in MLflow to ensure tokens are strictly for the current run."
            )
            return merged_any

        # 2. Try searching and aggregating across ALL telemetry runs in MLflow
        seen_stages = set()
        if git_slug or multi_repo:
            try:
                import mlflow
                from mlflow.tracking import MlflowClient
                from loaders.mlflow_asset_loader import MlFlowAssetLoader

                client = MlflowClient()
                experiment = MlFlowAssetLoader().get_or_create_experiment_by_name(
                    client, MlFlowAssetLoader.RESULT_ASSET_EXPERIMENT
                )

                filter_parts = ["tags.category = 'telemetry'", "tags.type = 'tokens'"]
                if git_slug:
                    filter_parts.append(f"tags.git_slug = '{git_slug}'")
                elif multi_repo:
                    filter_parts.append("tags.git_slug = 'multi-repo'")
                filter_string = " AND ".join(filter_parts)

                try:
                    runs = client.search_runs(
                        experiment_ids=[experiment.experiment_id],
                        filter_string=filter_string,
                        order_by=["attributes.start_time DESC"],
                    )
                except Exception:
                    quoted_parts = [
                        f'tags."{p.split(" = ")[0].split(".", 1)[1]}" = {p.split(" = ")[1]}'
                        for p in filter_parts
                    ]
                    runs = client.search_runs(
                        experiment_ids=[experiment.experiment_id],
                        filter_string=" AND ".join(quoted_parts),
                        order_by=["attributes.start_time DESC"],
                    )

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

