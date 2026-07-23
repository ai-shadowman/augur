import asyncio
import graphrag.api as api
from graphrag.config.load_config import load_config
from pathlib import Path
import logging
logging.basicConfig(level=logging.INFO)
import pandas as pd


class DependencyAnalyzer:
    """Query GraphRAG for dependency analysis"""

    def __init__(self, root_dir="."):

        self.root_dir = root_dir

        self._setup_search()

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
                             use_global: bool = True):
        """
        Use GraphRAG's LLM search to answer dependency questions

        Args:
            question (str): The question to ask
            retry_count (int, optional): Number of times to retry the query. Defaults to 3.
            use_global (bool, optional): Whether to use GraphRAG's global search.
            Defaults to True (recommended for many larger-scale code
            comprehension tasks).
        """

        num_tries_left = retry_count

        try:

            config = load_config(Path(self.root_dir))

            if use_global:

                result, _ = await api.global_search(
                    config=config,
                    entities=self.entity_df,
                    communities=self.communities_df,
                    community_reports=self.community_reports_df,
                    community_level=2,
                    response_type="Multiple Paragraphs",
                    query=question,
                    dynamic_community_selection=True,
                )

            else:

                result, _ = await api.global_search(
                    config=config,
                    entities=self.entity_df,
                    communities=self.communities_df,
                    community_reports=self.community_reports_df,
                    text_units=self.text_unit_df,
                    relationships=self.relationship_df,
                    covariates=None,
                    community_level=2,
                    response_type="Multiple Paragraphs",
                    query=question,
                )

        except Exception as e:

            num_tries_left -= 1

            if num_tries_left > 0:

                logging.info(f"Retrying query ({num_tries_left} tries left): {e}")

                return await self.query_with_llm(question, retry_count=num_tries_left, use_global=use_global)

            else:

                raise e

        return result

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

        report = ""

        questions = [
            """
            What migration order would be recommended when refactoring to reduce breaking changes?
            Provide a comprehensive order that includes as many classes, packages, and components as possible.
            Include the fully qualified names.
            """,
            """
            Are there any security vulnerabilities in the code or associated libraries?
            Include the fully qualified names.
            """,
            """
            Are there any dependencies that need to be upgraded and in what order?
            Include the fully qualified names.
            """
            ]

        for question in questions:

            result = await self.query_with_llm(question)

            if not result:

                raise Exception("Could not submit question at this time")

            report += f"### Question: {question}\n\n###Answer: {result}\n\n"

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
    
    
