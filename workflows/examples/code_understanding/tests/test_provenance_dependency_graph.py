"""The rendered dependency diagram: the cap, the level rule, and determinism.

The level cases are 0.txt's three worked examples, which the model was shown
and could get wrong, plus the fill that replaced its stop-before-overflow rule.
pandas is imported inside each function that builds a frame, never at module
scope, because conftest swaps the real one in only when these tests run.
"""

import re
import unittest

from provenance import dependency_graph as dg

EDGE = re.compile(r"n(\d+) --> n(\d+)")


def frames(counts, edges=()):
    """Entities named ``<TYPE>_<i>`` for each ``{type: count}``, and edges."""
    import pandas as pd

    rows = [(f"{kind}_{i}", kind) for kind, n in counts.items() for i in range(n)]
    entities = pd.DataFrame(rows, columns=["title", "type"])
    relationships = pd.DataFrame(list(edges), columns=["source", "target"])
    return entities, relationships


def mix(nodes):
    out = {}
    for _, kind in nodes:
        out[kind] = out.get(kind, 0) + 1
    return out


class TestSelection(unittest.TestCase):

    def test_levels(self):
        cases = [
            # 0.txt: "5 packages + 8 modules + 6 classes = 19 <= 20"; the
            # 20th slot now goes to a function.
            ({"PACKAGE": 5, "MODULE": 8, "CLASS": 6, "FUNCTION": 4},
             {"PACKAGE": 5, "MODULE": 8, "CLASS": 6, "FUNCTION": 1}),
            # 0.txt: "13 + 9 classes = 22 > 20"; 7 classes fill to the cap.
            ({"PACKAGE": 5, "MODULE": 8, "CLASS": 9},
             {"PACKAGE": 5, "MODULE": 8, "CLASS": 7}),
            # 0.txt: "18 packages, but 18 + 5 modules = 23 > 20".
            ({"PACKAGE": 18, "MODULE": 5}, {"PACKAGE": 18, "MODULE": 2}),
            # The dev index's shape: 4 packages, then modules fill.
            ({"PACKAGE": 4, "MODULE": 194, "CLASS": 337},
             {"PACKAGE": 4, "MODULE": 16}),
            # A missing top level is skipped, not counted.
            ({"MODULE": 3, "FUNCTION": 2}, {"MODULE": 3, "FUNCTION": 2}),
            # A level that lands exactly on the cap admits nothing below it.
            ({"PACKAGE": 20, "MODULE": 1}, {"PACKAGE": 20}),
            # Unlisted types never render.
            ({"LIBRARY": 9, "STDLIB_MODULE": 9}, {}),
        ]
        for counts, expected in cases:
            with self.subTest(counts=counts):
                nodes = dg.select_nodes(*frames(counts))
                self.assertEqual(mix(nodes), expected)
                self.assertLessEqual(len(nodes), dg.MAX_NODES)

    def test_a_partial_level_keeps_its_highest_degree_members(self):
        entities, relationships = frames(
            {"PACKAGE": 1, "MODULE": 4},
            [("MODULE_3", "PACKAGE_0"), ("MODULE_3", "MODULE_1"), ("MODULE_2", "MODULE_0")],
        )
        nodes = dg.select_nodes(entities, relationships, cap=3)
        # MODULE_3 has degree 2; MODULE_0, _1, _2 tie at 1 and break by title.
        self.assertEqual([t for t, _ in nodes], ["PACKAGE_0", "MODULE_3", "MODULE_0"])

    def test_type_case_is_folded(self):
        # GraphRAG uppercases types, but a lowercase straggler is in the dev
        # index (`library`), so a lowercase `module` must still count.
        import pandas as pd

        entities = pd.DataFrame([("a", "module"), ("b", None)], columns=["title", "type"])
        nodes = dg.select_nodes(entities, pd.DataFrame(columns=["source", "target"]))
        self.assertEqual(nodes, [("a", "MODULE")])

    def test_missing_and_non_string_titles_neither_crash_nor_render_as_nan(self):
        import pandas as pd

        entities = pd.DataFrame([(7, "MODULE"), (None, "MODULE"), ("b", "MODULE"), ("", "MODULE")],
                                columns=["title", "type"])
        nodes = dg.select_nodes(entities, pd.DataFrame(columns=["source", "target"]))
        self.assertEqual(nodes, [("7", "MODULE"), ("b", "MODULE")])


class TestRender(unittest.TestCase):

    def setUp(self):
        edges = [(f"MODULE_{i}", f"MODULE_{(i * 7) % 30}") for i in range(30)]
        edges += [("PACKAGE_0", "MODULE_3"), ("CLASS_1", "MODULE_2"), ("MODULE_4", "LIBRARY_0")]
        self.entities, self.relationships = frames(
            {"PACKAGE": 2, "MODULE": 30, "CLASS": 5, "LIBRARY": 1}, edges)

    def test_byte_identical_regardless_of_row_order(self):
        first = dg.render(self.entities, self.relationships)
        for seed in range(5):
            with self.subTest(seed=seed):
                again = dg.render(self.entities.sample(frac=1, random_state=seed),
                                  self.relationships.sample(frac=1, random_state=seed + 99))
                self.assertEqual(again, first)

    def test_every_edge_is_in_the_table_and_joins_rendered_nodes(self):
        out = dg.render(self.entities, self.relationships)
        titles = [t for t, _ in dg.select_nodes(self.entities, self.relationships)]
        table = set(zip(self.relationships.source, self.relationships.target, strict=True))
        drawn = [(titles[int(s)], titles[int(t)]) for s, t in EDGE.findall(out)]
        self.assertTrue(drawn)
        self.assertEqual([e for e in drawn if e not in table], [])
        self.assertEqual(len(re.findall(r"^    n\d+\[", out, re.M)), dg.MAX_NODES)

    def test_block_shape(self):
        out = dg.render(*frames({"PACKAGE": 1, "MODULE": 1}, [("PACKAGE_0", "MODULE_0")]))
        self.assertEqual(out, '```mermaid\ngraph TD\n    n0["PACKAGE_0"]\n'
                              '    n1["MODULE_0"]\n    n0 --> n1\n```')

    def test_label_escapes_quotes_and_flattens_whitespace(self):
        import pandas as pd

        entities = pd.DataFrame([('A "B"\nC', "MODULE")], columns=["title", "type"])
        out = dg.render(entities, pd.DataFrame(columns=["source", "target"]))
        self.assertIn('n0["A #quot;B#quot; C"]', out)

    def test_nothing_to_draw_says_so(self):
        self.assertIn("nothing to draw", dg.render(*frames({"LIBRARY": 3})))
        self.assertIn("nothing to draw", dg.render_repositories([]))


class TestRepositories(unittest.TestCase):

    def test_one_sorted_node_per_repository_and_no_edges(self):
        out = dg.render_repositories(
            ["https://github.com/b/y", "https://github.com/a/x", "https://github.com/b/y", ""])
        self.assertEqual(out, '```mermaid\ngraph TD\n    n0["https://github.com/a/x"]\n'
                              '    n1["https://github.com/b/y"]\n```')


if __name__ == "__main__":
    unittest.main()
