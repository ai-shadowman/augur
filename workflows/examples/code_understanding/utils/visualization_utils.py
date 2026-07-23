"""
NOTE: This class has a dependency on the pyvis library:
    pip install pyvis
"""

from graphrag.config.load_config import load_config
import logging
logging.basicConfig(level=logging.INFO)
import networkx as nx
import matplotlib.pyplot as plt
from pyvis.network import Network
from utils.graphrag_utils import DependencyAnalyzer
import traceback


def visualize_dependencies(analyzer: DependencyAnalyzer):
    """Create interactive dependency visualization from GraphRAG output"""

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

    net.save_graph('dependency_graph_interactive.html')

    logging.info(
        "Interactive graph saved to dependency_graph_interactive.html")

    plt.figure(figsize=(20, 16))

    pos = nx.spring_layout(G, k=2, iterations=50)

    node_colors = []

    for node in G.nodes():
        node_type = G.nodes[node].get('type', 'unknown')

        color_map = {
            'module': '#FF6B6B',
            'class': '#4ECDC4',
            'function': '#45B7D1',
            'package': '#96CEB4',
            'unknown': '#DFE6E9'
        }

        node_colors.append(color_map.get(node_type, '#DFE6E9'))

    nx.draw(G, pos,
            node_color=node_colors,
            node_size=1000,
            with_labels=True,
            font_size=8,
            font_weight='bold',
            arrows=True,
            edge_color='gray',
            alpha=0.7)

    # plt.title("Code Dependency Graph", fontsize=16)
    #
    # plt.tight_layout()
    #
    # plt.savefig('dependency_graph_static.png', dpi=300, bbox_inches='tight')
    #
    # logging.info("Static graph saved to dependency_graph_static.png")