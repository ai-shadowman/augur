import graphrag.api as api
from graphrag.config.load_config import load_config
from pathlib import Path
import os
import logging

logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())
import pandas as pd


class DependencyAnalyzer:
    """Query GraphRAG for dependency analysis"""

    def __init__(self, root_dir="."):

        self.root_dir = root_dir

        self._setup_configuration()

        self._setup_search()

        self._setup_prompts()

    def _setup_configuration(self):
        """Initialize instance configuration."""

        self.DYNAMIC_COMMUNITY_SELECTION_MINIMUM = 10

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

                return loader.download_prompt(path)

            except Exception as e:

                logging.warning(f"Could not preload prompt '{path}': {e}")

                return ""

        self.SYSTEM_PROMPT = _load("analysis/system-prompt/1")

        self.POST_AMBLE = _load("analysis/post-amble/json-format")

        self.RHEL_8to10_CONTEXT = _load("analysis/additional-context/rhel8-to-10")

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

    def detect_response_type(self, prompt: str) -> str:
        """Derive the GraphRAG response_type from a rendered prompt.
        """

        if not self.POST_AMBLE:

            logging.warning("JSON post-amble not loaded; defaulting response_type to 'Multiple Paragraphs'")

            return "Multiple Paragraphs"

        return "JSON" if self.POST_AMBLE.strip() in prompt else "Multiple Paragraphs"

    def detect_dynamic_community_selection(self) -> bool:
        """Determine whether to use dynamic community selection.
        """
        num_communities = len(self.communities_df)

        # Lower threshold to disable dynamic selection for more index sizes.
        logging.info(f"Community count: {num_communities} (minimum for dynamic community selection: {self.DYNAMIC_COMMUNITY_SELECTION_MINIMUM})")

        return num_communities > self.DYNAMIC_COMMUNITY_SELECTION_MINIMUM

    async def query_with_llm(self,
                             question: str,
                             retry_count: int = 3,
                             use_global: bool = True,
                             include_context: bool = False):
        """
        Use GraphRAG's LLM search to answer dependency questions

        Args:
            question (str): The question to ask
            retry_count (int, optional): Number of times to retry the query. Defaults to 3.
            use_global (bool, optional): Whether to use GraphRAG's global search.
            Defaults to True (recommended for many larger-scale code
            comprehension tasks).
            include_context (bool, optional): If True, return (result, context_data) tuple
            instead of just the result string. Defaults to False.
        """
        num_tries_left = retry_count

        try:

            config = load_config(Path(self.root_dir))

            response_type = self.detect_response_type(question)

            dynamic_community_selection = self.detect_dynamic_community_selection()

            if use_global:

                result, context_data = await api.global_search(
                    config=config,
                    entities=self.entity_df,
                    communities=self.communities_df,
                    community_reports=self.community_reports_df,
                    community_level=self.community_level,
                    response_type=response_type,
                    query=question,
                    dynamic_community_selection=dynamic_community_selection,
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

                return await self.query_with_llm(question, retry_count=num_tries_left,
                                                 use_global=use_global, include_context=include_context)

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

        prompts = [f"analysis/migration-report/{i}" for i in range(1, loader.num_prompts("analysis/migration-report") + 1)]

        answers = ["N/A"] * len(prompts)

        report = ""

        for i, prompt_path in enumerate(prompts):

            logging.info(f"Answers = {answers}")

            prompt = loader.download_prompt(
                prompt_path,
                system_prompt=self.SYSTEM_PROMPT,
                additional_context="",
                answers=answers,
                post_amble=self.POST_AMBLE,
            )

            result = await self.query_with_llm(prompt)

            if result:

                answers[i] = result

            question = loader.download_prompt(
                prompt_path,
                system_prompt="",
                additional_context="",
                answers=answers,
                post_amble="",
            )

            report += f"### Issue: {question}\n\n### Answer: {answers[i]}\n\n"

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
    
    
