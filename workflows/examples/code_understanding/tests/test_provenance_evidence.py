"""Every row grounding writes cites the chunks it was derived from.

The regression this guards is GraphRAG's ``create_communities``, which
explodes each relationship's ``text_unit_ids`` and sorts the result. A row
citing ``[]`` explodes to NaN and the sort raises, so a grounded graph could
not be clustered. ``test_ground_leaves_no_relationship_citing_nothing``
repeats that step on the tables ``ground`` returns.
"""

import unittest

from provenance import evidence, graph
from provenance.corpus import from_documents
from provenance.grounding_report import render

_HEADER = (
    '"""This file is located at {path} from repository url https://example.com/r.git, '
    'repository slug r, multi_repo false"""\n'
)
FILES = {
    "pkg/sub/a.py": ("d1", ["u1", "u2"], "class A(object):\n    def run(self):\n        pass\n"),
    "pkg/sub/b.py": ("d2", ["u3"], "def helper():\n    pass\n"),
}


def documents(files=FILES, with_units=True):
    import pandas as pd

    rows = []
    for path, (doc_id, units, body) in files.items():
        row = {"id": doc_id, "title": path, "text": _HEADER.format(path=path) + body}
        if with_units:
            row["text_unit_ids"] = units
        rows.append(row)
    return pd.DataFrame(rows)


def entities(rows):
    """``(title, type, text_unit_ids)`` triples."""
    import pandas as pd

    return pd.DataFrame(
        [{"title": t, "type": k, "description": "", "text_unit_ids": u} for t, k, u in rows]
    )


def edges(rows=()):
    import pandas as pd

    return pd.DataFrame(
        list(rows), columns=["source", "target", "description", "weight", "text_unit_ids"]
    )


#: What the model extracted: b.py's module entity is missing, so grounding
#: creates it, and ``pkg.sub`` is a directory nobody named.
EXTRACTED = [
    ("PKG/SUB/A.PY", "MODULE", ["u1"]),
    ("PKG.SUB.A.A", "CLASS", ["u1"]),
    ("PKG.SUB.A.A.RUN", "FUNCTION", ["u2"]),
    ("PKG.SUB.B.HELPER", "FUNCTION", ["u3"]),
]


def ground(docs=None):
    docs = documents() if docs is None else docs
    return graph.ground(from_documents(docs), entities(EXTRACTED), edges(), documents=docs)


def cited(frame, title_column, title):
    return list(frame.loc[frame[title_column] == title, "text_unit_ids"].iloc[0])


class TestWhatEachRowCites(unittest.TestCase):
    def test_units_by_document_keys_on_the_header_path(self):
        self.assertEqual(evidence.units_by_document(documents()),
                         {"pkg/sub/a.py": ["u1", "u2"], "pkg/sub/b.py": ["u3"]})

    def test_a_created_file_cites_its_own_document(self):
        ents, _, report = ground()
        self.assertIn("PKG/SUB/B.PY", report.created)
        self.assertEqual(cited(ents, "title", "PKG/SUB/B.PY"), ["u3"])

    def test_a_synthesized_package_cites_the_documents_under_it(self):
        ents, _, report = ground()
        self.assertIn("PKG.SUB", report.packages_created)
        self.assertEqual(sorted(cited(ents, "title", "PKG.SUB")), ["u1", "u2", "u3"])

    def test_belongs_to_cites_its_source_entity(self):
        _, rels, _ = ground()
        row = rels[(rels["source"] == "PKG/SUB/A.PY") & rels["description"].str.contains("BELONGS_TO")]
        self.assertEqual(list(row["text_unit_ids"].iloc[0]), ["u1"])

    def test_defined_in_cites_the_declaring_document_not_its_endpoints(self):
        _, rels, _ = ground()
        row = rels[(rels["source"] == "PKG.SUB.A.A.RUN") & rels["description"].str.contains("DEFINED_IN")]
        self.assertEqual(list(row["text_unit_ids"].iloc[0]), ["u1", "u2"])

    def test_the_repository_cites_nothing(self):
        ents, _, report = ground()
        self.assertEqual(cited(ents, "title", report.repository), [])

    def test_the_report_counts_and_renders_it(self):
        _, _, report = ground()
        self.assertGreater(report.evidence_entities, 0)
        self.assertGreater(report.evidence_relationships, 0)
        self.assertIn("chunks cited on written rows", render(report))


class TestTheRegression(unittest.TestCase):
    def test_ground_leaves_no_relationship_citing_nothing(self):
        """``create_communities``: explode, then ``sorted(set(...))``."""
        _, rels, _ = ground()
        self.assertTrue(len(rels))
        exploded = rels.explode("text_unit_ids")["text_unit_ids"]
        self.assertFalse(exploded.isna().any(), list(rels.loc[rels["text_unit_ids"].map(len) == 0, "description"]))
        sorted(set(exploded))

    def test_numpy_arrays_as_parquet_returns_them(self):
        """A table read back from parquet holds arrays, not lists."""
        import numpy as np

        docs = documents()
        docs["text_unit_ids"] = [np.array(u) for u in docs["text_unit_ids"]]
        # Two chunks each, since a one-element array has an unambiguous truth
        # value. The VARIABLE row matters: module rows come back from
        # ``ground_modules`` as lists, and a variable's ``BELONGS_TO`` is
        # backfilled from the array the extraction left.
        rows = [*EXTRACTED, ("PKG.SUB.A.SETTING", "VARIABLE", ["u1"])]
        ents = entities([(t, k, np.array([*u, "u9"])) for t, k, u in rows])
        _, rels, _ = graph.ground(from_documents(docs), ents, edges(), documents=docs)
        self.assertFalse(rels.explode("text_unit_ids")["text_unit_ids"].isna().any())

    def test_documents_without_chunk_ids_leave_the_tables_as_they_were(self):
        docs = documents(with_units=False)
        ents, _rels, report = ground(docs)
        self.assertEqual(report.evidence_entities, 0)
        self.assertEqual(report.evidence_relationships, 0)
        self.assertEqual(cited(ents, "title", "PKG.SUB"), [])


if __name__ == "__main__":
    unittest.main()
