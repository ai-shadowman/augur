import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))


from utils.otel_utils import enable_telemetry


##############################################################################
# Pipeline stage
##############################################################################

class AnalysisPipeline:

    @enable_telemetry
    def run(self, graphrag_source_path: str, git_repo: str = "", git_branch: str = "",
            multi_repo: bool = False):
        """Generates a migration report from the GraphRAG index and returns the result."""
        import asyncio, logging
        from loaders.default_asset_loader import DefaultAssetLoader
        from utils.graphrag_utils import DependencyAnalyzer
        from pipelines.base.data_generation import generate_git_slug

        logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

        git_slug = generate_git_slug(git_repo, git_branch) if git_repo else None

        analyzer = DependencyAnalyzer(graphrag_source_path, git_slug=git_slug or "", multi_repo=multi_repo)

        try:
            from utils.duration_tracker import DurationTracker
            dur_tracker = DurationTracker.get_instance()
            dur_tracker.download_from_mlflow(git_slug=git_slug, multi_repo=multi_repo)
            for check_path in [graphrag_source_path, os.path.join(graphrag_source_path, "output")]:
                dur_file = os.path.join(check_path, "durations.json")
                if os.path.exists(dur_file):
                    dur_tracker.load_and_merge(dur_file)
                    break
        except Exception:
            dur_tracker = None

        try:
            analyzer.token_tracker.download_from_mlflow(git_slug=git_slug, multi_repo=multi_repo)
            for check_path in [graphrag_source_path, os.path.join(graphrag_source_path, "output")]:
                tokens_file = os.path.join(check_path, "tokens.json")
                if os.path.exists(tokens_file):
                    from utils.token_tracker import TokenCostTracker
                    analyzer.token_tracker.merge(TokenCostTracker.load_from_file(tokens_file))
                    break
        except Exception:
            pass

        if dur_tracker:
            with dur_tracker.measure(stage="Analysis", step="Generate Migration Report"):
                report = asyncio.run(analyzer.generate_migration_report())
        else:
            report = asyncio.run(analyzer.generate_migration_report())

        # Safeguard: ensure duration summary is present in the markdown report
        if dur_tracker and "### Pipeline Execution Duration Summary" not in report:
            dur_md = dur_tracker.format_markdown_section()
            if dur_md.strip():
                import re
                match = re.search(r'(#+\s*Code\s+Migration\s+Plan\s*\(?JSON\)?)', report, re.IGNORECASE)
                if match:
                    idx = match.start()
                    report = report[:idx] + dur_md.strip() + "\n\n" + report[idx:]
                else:
                    report = f"{report.rstrip()}\n\n{dur_md.strip()}\n"

        try:
            analyzer.token_tracker.upload_to_mlflow(git_slug=git_slug, stage="Analysis", multi_repo=multi_repo)
        except Exception as e:
            logging.debug(f"Failed to upload token metrics to MLflow: {e}")

        if dur_tracker:
            try:
                dur_tracker.upload_to_mlflow(git_slug=git_slug, stage="Analysis", multi_repo=multi_repo)
            except Exception as e:
                logging.debug(f"Failed to upload duration metrics to MLflow: {e}")


        result_file = f"migration_report_{git_slug}.md" if git_slug else "migration_report.md"

        DefaultAssetLoader().log_results(

            result_file,

            artifact_path=DefaultAssetLoader.get_log_results_artifact_path(

                DefaultAssetLoader.RESULTS_PATH_PREFIX_PIPELINES,

                git_slug=git_slug,

                multi_repo=multi_repo,

            ),

            content=report,

            tags={"git_slug": git_slug, "multi_repo": multi_repo, "category": "analysis"},

        )

        return report

    def run_multi_repo(self):
        """Runs migration report generation across the combined multi-repo GraphRAG index."""
        import os, logging
        from loaders.default_asset_loader import DefaultAssetLoader
        from utils.loader_utils import download_result_directory

        logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

        graphrag_source_path = os.getenv("KFP_DATA_INDEXING_OUTPUT_PATH", "graph_rag_app/source")

        logging.info("Downloading multi-repo GraphRAG index...")
        download_result_directory(
            git_slug=None,
            download_dir=os.path.join(graphrag_source_path, "output"),
            results_prefix=DefaultAssetLoader.RESULTS_PATH_PREFIX_REPO_DATASETS,
            multi_repo=True,
            asset_tags={"multi_repo": True, "category": "indexing"},
        )

        return self.run(graphrag_source_path=graphrag_source_path, multi_repo=True)

    @enable_telemetry
    def run_adhoc_query(
        self,
        question: str,
        retry_count: int = 3,
        use_global: bool = True,
        git_repo: str = "",
        git_branch: str = "main",
        multi_repo: bool = False,
    ):
        """Queries the GraphRAG index with an LLM and returns the result."""
        import asyncio, logging
        from datetime import datetime
        from loaders.default_asset_loader import DefaultAssetLoader
        from utils.graphrag_utils import DependencyAnalyzer
        from pipelines.base.data_generation import generate_git_slug

        logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

        use_multi_repo = multi_repo or not git_repo

        git_slug = generate_git_slug(git_repo, git_branch) if git_repo else ""

        graphrag_source_path = "graph_rag_app/source"

        adhoc_results_header = "\n---\n\n# ##################ADHOC RESULTS##################\n"

        try:
            DependencyAnalyzer.download_graphrag_directory(
                download_dir=graphrag_source_path,
                git_slug=git_slug,
                multi_repo=use_multi_repo,
                git_repo=git_repo,
            )
        except Exception:
            msg = (
                "Could not perform query: "
                + ("no multi-repository index was found" if use_multi_repo else
                   f"no index was found (git_repo='{git_repo}')")
                + ". Maybe you need to generate it first?"
            )
            logging.error(msg)
            print(adhoc_results_header, flush=True)
            return msg

        analyzer = DependencyAnalyzer(graphrag_source_path, git_slug=git_slug, multi_repo=use_multi_repo)

        postamble = "Provide as much detail as possible. Include the git repo url(s) in the report."

        if use_multi_repo:
            postamble += (" Include ALL the git repo urls that you can find."
                          " Group git repositories by their git repo url.")

        try:
            from utils.duration_tracker import DurationTracker
            dur_tracker = DurationTracker.get_instance()
        except Exception:
            dur_tracker = None

        if dur_tracker:
            with dur_tracker.measure(stage="Analysis", step="Adhoc Query"):
                result = asyncio.run(analyzer.query_with_llm(
                    question + postamble,
                    retry_count=retry_count,
                    use_global=use_global,
                    response_type="Multiple Paragraphs, plain text, no markdown formatting",
                ))
        else:
            result = asyncio.run(analyzer.query_with_llm(
                question + postamble,
                retry_count=retry_count,
                use_global=use_global,
                response_type="Multiple Paragraphs, plain text, no markdown formatting",
            ))

        try:
            analyzer.token_tracker.log_to_mlflow()
        except Exception as e:
            logging.debug(f"Failed to log token metrics to MLflow: {e}")

        if dur_tracker:
            try:
                dur_tracker.log_to_mlflow()
            except Exception as e:
                logging.debug(f"Failed to log duration metrics to MLflow: {e}")

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

        result_file = f"adhoc_query_{timestamp}.txt"

        DefaultAssetLoader().log_results(

            result_file,

            artifact_path=(

                DefaultAssetLoader.get_log_results_artifact_path(

                    DefaultAssetLoader.RESULTS_PATH_PREFIX_ADHOC_QUERIES,

                    git_slug=git_slug,

                    multi_repo=use_multi_repo,

                )

            ),

            content=f"Question: {question}\n\nAnswer:\n{result}",

            tags={"category": "analysis",
                  "adhoc_query": "true",
                  "git_slug": git_slug,
                  "multi_repo": use_multi_repo},

        )

        print(adhoc_results_header, flush=True)
        return f"{result}\n\n"


##############################################################################
# Helpers
##############################################################################

def write_migration_report(graphrag_source_path: str, report_path: str,
                           git_repo: str = "", git_branch: str = "",
                           multi_repo: bool = False):
    """Run the migration report and write the result to report_path."""
    import os
    migration_report = AnalysisPipeline().run(graphrag_source_path, git_repo=git_repo,
                                              git_branch=git_branch, multi_repo=multi_repo)
    if dirname := os.path.dirname(report_path):
        os.makedirs(dirname, exist_ok=True)
    with open(report_path, "w") as f:
        f.write(migration_report)


##############################################################################
# Module-level aliases for external callers (notebooks, scripts)
##############################################################################

def run_full_pipeline(*args, **kwargs):
    return AnalysisPipeline().run(*args, **kwargs)

def run_full_pipeline_multi_repo(*args, **kwargs):
    return AnalysisPipeline().run_multi_repo(*args, **kwargs)

def run_adhoc_query_pipeline(*args, **kwargs):
    return AnalysisPipeline().run_adhoc_query(*args, **kwargs)
