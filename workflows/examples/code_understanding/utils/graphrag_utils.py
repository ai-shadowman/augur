import graphrag.api as api
from graphrag.config.load_config import load_config
from pathlib import Path
import os
import logging

logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())
import pandas as pd


class DependencyAnalyzer:
    """Query GraphRAG for dependency analysis"""

    def __init__(self, root_dir=".", git_slug: str = "", multi_repo: bool = False):

        self.root_dir = root_dir

        self.git_slug = git_slug

        self.multi_repo = multi_repo

        self._setup_configuration()

        self._setup_search()

        self._setup_prompts()

    def _setup_configuration(self):
        """Initialize instance configuration."""

    def _setup_search(self):
        """Initialize GraphRAG search"""
        entity_df = pd.read_parquet(f"{self.root_dir}/output/entities.parquet")

        relationship_df = pd.read_parquet(f"{self.root_dir}/output/relationships.parquet")

        text_unit_df = pd.read_parquet(f"{self.root_dir}/output/text_units.parquet")

        communities_df = pd.read_parquet(f"{self.root_dir}/output/communities.parquet")

        community_reports_df = pd.read_parquet(f"{self.root_dir}/output/community_reports.parquet")

        self.entity_df = entity_df

        self.relationship_df = relationship_df

        self.text_unit_df = text_unit_df

        self.communities_df = communities_df

        self.community_reports_df = community_reports_df

        self.community_level = (
            int(community_reports_df["level"].max())
            if not community_reports_df.empty and "level" in community_reports_df.columns
            else 0
        )



    def _setup_prompts(self):
        """Pre-load static prompt assets."""

        from loaders.default_asset_loader import DefaultAssetLoader

        loader = DefaultAssetLoader()

        def _load(path):

            try:

                prompt, _ = loader.download_prompt(path)

                return prompt

            except Exception as e:

                logging.warning(f"Could not preload prompt '{path}': {e}")

                return ""

        self.SYSTEM_PROMPT_DATA_EXTRACTION = _load(
            "analysis/system-prompt/data-extraction")

        self.SYSTEM_PROMPT_RHEL_ADMIN = _load(
            "analysis/system-prompt/rhel-admin")

        self.POST_AMBLE = _load(
            "analysis/post-amble/json-format")

        self.RHEL_8to10_CONTEXT = _load(
            "analysis/additional-context/rhel8-to-10")

    def _find_dependencies(self, module_name):
        """Find all dependencies for a given module"""

        deps = self.relationship_df[
            (self.relationship_df['source'].str.contains(module_name,
                                                         case=False)) &
            (self.relationship_df['description'].str.contains('import|depend',
                                                              case=False))
            ]

        results = []

        for _, row in deps.iterrows():
            results.append({
                'from': row['source'],

                'to': row['target'],

                'type': row['description'],

                'weight': row.get('weight', 1.0)
            })

        return results

    def _find_dependents(self, module_name):
        """Find all modules that depend on this module"""

        deps = self.relationship_df[

            (self.relationship_df['target'].str.contains(module_name,
                                                         case=False)) &
            (self.relationship_df['description'].str.contains('import|depend',
                                                              case=False))
            ]

        results = []

        for _, row in deps.iterrows():

            results.append({

                'from': row['source'],

                'to': row['target'],

                'type': row['description'],

                'weight': row.get('weight', 1.0)
            })

        return results

    def _find_circular_dependencies(self):
        """Detect circular dependencies"""

        graph = {}

        for _, row in self.relationship_df.iterrows():

            source = row['source']

            target = row['target']

            if source not in graph:

                graph[source] = []

            graph[source].append(target)

        def _has_cycle(node, visited, rec_stack, path):

            visited.add(node)

            rec_stack.add(node)

            path.append(node)

            if node in graph:

                for neighbor in graph[node]:

                    if neighbor not in visited:

                        if _has_cycle(neighbor, visited, rec_stack, path):

                            return True

                    elif neighbor in rec_stack:

                        cycle_start = path.index(neighbor)

                        return path[cycle_start:]

            path.pop()

            rec_stack.remove(node)

            return False

        visited = set()

        cycles = []

        for node in graph:

            if node not in visited:

                rec_stack = set()

                path = []

                cycle = _has_cycle(node, visited, rec_stack, path)

                if cycle:

                    cycles.append(cycle)

        return cycles

    def _get_dependency_layers(self):
        """Identify architectural layers based on dependencies"""
        in_degree = {}

        for _, row in self.relationship_df.iterrows():

            target = row['target']

            in_degree[target] = in_degree.get(target, 0) + 1

        layers = {}

        for entity in self.entity_df['title']:

            if entity not in in_degree:

                layers[entity] = 0

        sorted_entities = sorted(in_degree.items(), key=lambda x: x[1])

        return {
            'leaf_modules': [k for k, v in sorted_entities[:5]],

            'intermediate_modules': [k for k, v in sorted_entities[5:15]],

            'top_modules': [k for k, v in sorted_entities[-5:]]
        }

    async def query_with_llm(self,
                             question: str,
                             retry_count: int = 3,
                             use_global: bool = True,
                             include_context: bool = False,
                             bypass_index: bool = False):
        """
        Use LLM to answer a question.

        Args:
            question (str): The question to ask
            retry_count (int, optional): Number of times to retry the query. Defaults to 3.
            use_global (bool, optional): Whether to use GraphRAG's global search.
            Defaults to True (recommended for many larger-scale code
            comprehension tasks). Ignored when bypass_index is True.
            include_context (bool, optional): If True, return (result, context_data) tuple
            instead of just the result string. Defaults to False.
            bypass_index (bool, optional): If True, send the question directly to the
            configured LLM without using the GraphRAG index. Defaults to False.
        """
        num_tries_left = retry_count

        try:

            config = load_config(Path(self.root_dir))

            if bypass_index:

                logging.debug(f"Bypassing index for prompt={question}. Sending question directly to LLM...")

                from graphrag.language_model.manager import ModelManager

                llm_config = config.models.get("default_chat_model")

                if llm_config is None:
                    raise KeyError("No model named 'default_chat_model' found in config.models")

                chat_model = ModelManager().get_or_create_chat_model(
                    name="default_chat_model",
                    model_type=llm_config.type,
                    config=llm_config,
                )

                response = await chat_model.achat(question)

                result = response.output.content

                logging.debug(f"Raw LLM response with bypass_index=True: {response.output.content}")

                context_data = None

            else:

                response_type = "Multiple Paragraphs"

                if use_global:

                    result, context_data = await api.global_search(
                        config=config,
                        entities=self.entity_df,
                        communities=self.communities_df,
                        community_reports=self.community_reports_df,
                        community_level=self.community_level,
                        response_type=response_type,
                        query=question,
                        dynamic_community_selection=False,
                    )

                else:

                    result, context_data = await api.local_search(
                        config=config,
                        entities=self.entity_df,
                        community_reports=self.community_reports_df,
                        text_units=self.text_unit_df,
                        relationships=self.relationship_df,
                        covariates=None,
                        community_level=self.community_level,
                        response_type=response_type,
                        query=question,
                    )

        except Exception as e:

            num_tries_left -= 1

            if num_tries_left > 0:

                logging.info(f"Retrying query ({num_tries_left} tries left): {e}")

                return await self.query_with_llm(question,
                                                 retry_count=num_tries_left,
                                                 use_global=use_global,
                                                 include_context=include_context,
                                                 bypass_index=bypass_index)

            else:

                raise e

        if include_context:
            return result, context_data

        return result

    @staticmethod
    def extract_context_content(context_data) -> str:
        """Extracts and concatenates the full_content column from GraphRAG context_data.

        Args:
            context_data: The context_data dict returned by query_with_llm when
                include_context=True. Current version expects a "reports"
                key holding a DataFrame with a "full_content" column.

        Returns:
            A single string of all report full_content values joined by a separator,
            or an empty string if context_data is missing or contains no reports.
        """
        if not isinstance(context_data, dict):
            return ""

        reports = context_data.get("reports", pd.DataFrame())

        if reports.empty or "full_content" not in reports.columns:
            return ""

        return "\n\n---\n\n".join(reports["full_content"].dropna().astype(str).tolist())

    def raw_data(self):
        """Return the raw dataframes used for analysis"""
        return {"entities": self.entity_df,
                "relationships": self.relationship_df,
                "communities": self.communities_df,
                "community_reports": self.community_reports_df,
                "text_units": self.text_unit_df
        }

    async def generate_migration_report(self):
        """Generate a high-level migration report"""

        from loaders.default_asset_loader import DefaultAssetLoader

        loader = DefaultAssetLoader()

        graphrag_prompts = [f"analysis/migration-report/{i}" for i in range(loader.num_prompts("analysis/migration-report"))]

        enhanced_prompts = [f"analysis/migration-report/enhanced/{i}" for i in range(loader.num_prompts("analysis/migration-report/enhanced"))]

        prompts = graphrag_prompts + enhanced_prompts

        answers = ["N/A"] * len(prompts)

        report = ""

        for i, prompt_path in enumerate(prompts):

            is_enhanced = prompt_path.startswith("analysis/migration-report/enhanced")

            logging.debug(f"answers = {answers}")

            logging.info(f"Generating report for prompt {prompt_path}...")

            prompt, meta = loader.download_prompt(
                prompt_path,
                system_prompt_data_extraction=self.SYSTEM_PROMPT_DATA_EXTRACTION,
                system_prompt_rhel_admin=self.SYSTEM_PROMPT_RHEL_ADMIN,
                additional_context=self.RHEL_8to10_CONTEXT,
                answers=answers,
                post_amble=self.POST_AMBLE,
            )

            result = await self.query_with_llm(prompt, bypass_index=is_enhanced)

            result = f"{meta.get('title')}\n\n{result}"

            answers[i] = result

            report += f"{result}\n\n"

        return report
    
    async def generate_report(self, service_name: str):

        deps = self._find_dependencies(service_name)

        logging.info(f"Dependencies for {service_name}:")

        for dep in deps:

            logging.info(f"  {dep['from']} -> {dep['to']} ({dep['type']})")

        dependents = self._find_dependents("database")

        logging.info("\nModules depending on database:")

        for dep in dependents:

            logging.info(f"  {dep['from']} -> {dep['to']}")

        # cycles = self._find_circular_dependencies()

        # if cycles:

        #     logging.info("\n⚠️  Circular dependencies found:")

        #     print(cycles)

        #     for cycle in cycles:

        #         logging.info(f"  {' -> '.join(cycle)}")

        layers = self._get_dependency_layers()

        logging.info("\nArchitectural Layers:")

        logging.info(f"  Leaf modules (no dependencies): {layers['leaf_modules']}")

        logging.info(f"  Top modules (many dependencies): {layers['top_modules']}")
    
    
