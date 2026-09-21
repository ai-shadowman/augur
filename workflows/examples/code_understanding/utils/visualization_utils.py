"""
NOTE: This class has a dependency on the pyvis library:
    pip install pyvis
"""

from graphrag.config.load_config import load_config
import logging
import os
logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())
import networkx as nx
import matplotlib.pyplot as plt
from pyvis.network import Network
from utils.graphrag_utils import DependencyAnalyzer
import traceback


def visualize_dependencies(analyzer: DependencyAnalyzer):
    """Create interactive dependency visualization from GraphRAG output"""

    try:

        G = nx.DiGraph()

        for _, row in analyzer.entity_df.iterrows():
            G.add_node(

                row['title'],

                type=row.get('type', 'unknown'),

                description=row.get('description', '')
            )

        for _, row in analyzer.relationship_df.iterrows():

            if 'import' in row['description'].lower() or 'depend' in row[
                'description'].lower():
                G.add_edge(

                    row['source'],

                    row['target'],

                    relationship=row['description'],

                    weight=row.get('weight', 1.0)
                )

        net = Network(height='800px', width='100%', directed=True)

        net.from_nx(G)

        net.show_buttons(filter_=['physics'])

        html_path = 'dependency_graph_interactive.html'

        net.save_graph(html_path)

        logging.info(f"Interactive graph saved to {html_path}")

        return html_path

    except Exception as e:

        logging.error(f"Error generating dependency visualization: {e}")

        logging.error(traceback.format_exc())

        return None


def log_interactive_dependency_graph(analyzer: DependencyAnalyzer):
    """Generates the interactive dependency graph and logs it as an artifact via DefaultAssetLoader."""
    from loaders.default_asset_loader import DefaultAssetLoader

    html_path = visualize_dependencies(analyzer)

    if html_path is None:

        logging.warning("Skipping artifact logging: dependency graph was not generated.")

        return

    artifact_path = DefaultAssetLoader.get_log_results_artifact_path(

        DefaultAssetLoader.RESULTS_PATH_PREFIX_VISUALIZATIONS,

        git_slug=analyzer.git_slug,

        multi_repo=analyzer.multi_repo,

    )

    DefaultAssetLoader().log_results(html_path,

                                     artifact_path=artifact_path,

                                     tags={"category": "visualization", "git_slug": analyzer.git_slug})