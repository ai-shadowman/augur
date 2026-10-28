"""The relationship type field, from the model's tuple to the finalised column.

Relationship layer Phase 9. The parser half needs graphrag and networkx,
which this repository's environment deliberately lacks, so those cases skip
here and run under ``~/Developer/augur-test/.venv``, which pins graphrag 2.6.0:

    ~/Developer/augur-test/.venv/bin/python -m pytest tests/test_provenance_typed_extract.py

Everything after the parser (claim tokens, the cross-chunk merge, the type
and subject read from claims, grounding's duplicate merge, the join after
``finalize_relationships``) is pure and runs everywhere.
"""

import asyncio
import importlib.util
import unittest

from provenance import edge_types, typed_extract
from provenance.grounding_report import GroundingReport

HAS_GRAPHRAG = importlib.util.find_spec("graphrag") is not None
SEP = "\x1f"


def frame(rows):
    import pandas as pd

    return pd.DataFrame(rows)


class TestClaims(unittest.TestCase):
    def test_relative_tokens_follow_the_row_columns(self):
        claims = [f"A{SEP}IMPORTS{SEP}B", f"B{SEP}CALLS{SEP}A", f"C{SEP}USES{SEP}D"]
        self.assertEqual(typed_extract.relative_claims(claims, "A", "B"), ["IMPORTS>", "CALLS<"])
        self.assertEqual(typed_extract.relative_claims(claims, "B", "A"), ["IMPORTS<", "CALLS>"])
        self.assertEqual(typed_extract.relative_claims(None, "A", "B"), [])

    def test_type_and_subject_come_from_valid_claims(self):
        cases = [
            # (claims, description, type, subject)
            (["CALLS>"], "A USES B", "CALLS", edge_types.SOURCE),
            (["IMPORTS<"], "", "IMPORTS", edge_types.TARGET),
            (["CALLS>", "CALLS<"], "", "CALLS", edge_types.BOTH),
            # The most specific type wins, and its own direction decides.
            (["USES>", "INHERITS<"], "", "INHERITS", edge_types.TARGET),
            # A field outside the vocabulary is no claim: the prose decides.
            (["RELATES_TO>"], "A IMPORTS B", "IMPORTS", edge_types.SOURCE),
            ([], "A IMPORTS B", "IMPORTS", edge_types.SOURCE),
            (None, "A IMPORTS B", "IMPORTS", edge_types.SOURCE),
            (float("nan"), "B CALLS A", "CALLS", edge_types.TARGET),
        ]
        for claims, description, want_type, want_subject in cases:
            with self.subTest(claims=claims, description=description):
                rows = frame([{"source": "A", "target": "B", "description": description,
                               edge_types.CLAIMS_COLUMN: claims}])
                typed = edge_types.with_subject(edge_types.with_type(rows))
                self.assertEqual((typed[edge_types.TYPE_COLUMN][0], typed[edge_types.SUBJECT_COLUMN][0]),
                                 (want_type, want_subject))

    def test_a_frame_without_the_column_reads_as_before(self):
        rows = frame([{"source": "A", "target": "B", "description": "A INHERITS B"}])
        self.assertEqual(edge_types.with_type(rows)[edge_types.TYPE_COLUMN][0], "INHERITS")

    def test_the_cross_chunk_merge_keeps_every_claim(self):
        one = frame([{"source": "A", "target": "B", "description": "d1", "source_id": "c1", "weight": 1.0,
                      edge_types.CLAIMS_COLUMN: ["IMPORTS>"]}])
        two = frame([{"source": "A", "target": "B", "description": "d2", "source_id": "c2", "weight": 2.0,
                      edge_types.CLAIMS_COLUMN: ["CALLS<"]}])
        old = frame([{"source": "C", "target": "D", "description": "d3", "source_id": "c3", "weight": 1.0}])
        merged = typed_extract.merge_relationships([one, two, old]).set_index(["source", "target"])
        self.assertEqual(list(merged.loc[("A", "B"), edge_types.CLAIMS_COLUMN]), ["IMPORTS>", "CALLS<"])
        self.assertEqual(merged.loc[("A", "B"), "weight"], 3.0)
        self.assertEqual(list(merged.loc[("C", "D"), edge_types.CLAIMS_COLUMN]), [])

    def test_grounding_merges_claims_with_duplicate_rows(self):
        from provenance.tables import merge_duplicate_edges

        rows = frame([
            {"source": "A", "target": "B", "description": "x", "text_unit_ids": ["1"], "weight": 1.0,
             edge_types.CLAIMS_COLUMN: ["IMPORTS>"]},
            {"source": "A", "target": "B", "description": "y", "text_unit_ids": ["2"], "weight": 1.0,
             edge_types.CLAIMS_COLUMN: ["CALLS>", "IMPORTS>"]},
        ])
        merged = merge_duplicate_edges(rows, GroundingReport())
        self.assertEqual(list(merged[edge_types.CLAIMS_COLUMN][0]), ["IMPORTS>", "CALLS>"])

    def test_the_join_after_finalise(self):
        from provenance.pipeline import with_claims

        final = frame([{"id": "1", "source": "A", "target": "B", "description": "A USES B"},
                       {"id": "2", "source": "C", "target": "D", "description": "C DEFINED_IN D"}])
        claims = frame([{"source": "A", "target": "B", edge_types.CLAIMS_COLUMN: ["CALLS>"]},
                        {"source": "C", "target": "D", edge_types.CLAIMS_COLUMN: float("nan")}])
        joined = with_claims(final, claims)
        self.assertEqual(list(joined[edge_types.TYPE_COLUMN]), ["CALLS", "DEFINED_IN"])
        self.assertEqual([list(c) for c in joined[edge_types.CLAIMS_COLUMN]], [["CALLS>"], []])


@unittest.skipUnless(HAS_GRAPHRAG, "graphrag is not installed here; run under augur-test/.venv")
class TestParser(unittest.TestCase):
    def test_graphrag_is_the_version_the_adapted_code_was_read_from(self):
        from importlib.metadata import version

        self.assertEqual(version("graphrag"), typed_extract.GRAPHRAG_VERSION)

    def test_six_and_five_field_records(self):
        extractor = typed_extract._graph_extractor()(model_invoker=None)
        output = (
            '("entity"<|>A<|>module<|>a)##'
            '("relationship"<|>A<|>B<|>IMPORTS<|>A IMPORTS B<|>8)##'
            '("relationship"<|>B<|>A<|>calls<|>B CALLS A<|>3)##'
            '("relationship"<|>C<|>D<|>C USES D, the old tuple<|>5)##'
        )
        graph = asyncio.run(extractor._process_results({0: output}, "<|>", "##"))
        ab = graph.get_edge_data("A", "B")
        self.assertEqual(len(ab["description"].split("\n")), 2)
        self.assertEqual(sorted(ab[edge_types.CLAIMS_COLUMN]), sorted([f"A{SEP}IMPORTS{SEP}B", f"B{SEP}CALLS{SEP}A"]))
        self.assertEqual(ab["weight"], 11.0)
        cd = graph.get_edge_data("C", "D")
        self.assertEqual((cd["description"], cd[edge_types.CLAIMS_COLUMN]), ("C USES D, the old tuple", []))


    def test_the_strategy_turns_claims_relative_to_each_row(self):
        """``run_typed`` end to end, with a chat model that returns one canned answer."""
        from types import SimpleNamespace
        from unittest import mock

        from graphrag.index.operations.extract_graph.typing import Document

        answer = ('("entity"<|>PKG.A<|>module<|>a)##("entity"<|>PKG.B<|>module<|>b)##'
                  '("relationship"<|>PKG.B<|>PKG.A<|>INHERITS<|>PKG.B INHERITS PKG.A<|>9)##'
                  '("relationship"<|>PKG.A<|>PKG.B<|>CALLS<|>PKG.A CALLS PKG.B<|>2)##')

        class Model:
            async def achat(self, prompt, history=None, **kwargs):
                return SimpleNamespace(output=SimpleNamespace(content=answer), history=[])

        llm = {"type": "chat", "model_provider": "openai", "model": "x", "api_key": "x",
               "tokens_per_minute": None, "requests_per_minute": None}
        with mock.patch("graphrag.language_model.manager.ModelManager.get_or_create_chat_model",
                        return_value=Model()):
            result = asyncio.run(typed_extract.run_typed(
                [Document(text="t", id="chunk-1")], ["module"], None,
                {"llm": llm, "max_gleanings": 0, "extraction_prompt": "{input_text}"}))
        rel = result.relationships
        self.assertEqual(len(rel), 1)
        row = rel.iloc[0]
        # Whichever way to_pandas_edgelist oriented the row, PKG.B inherits and PKG.A calls.
        claims = {("PKG.B", "INHERITS"), ("PKG.A", "CALLS")}
        read = {(row["source"] if t.endswith(">") else row["target"], t[:-1]) for t in row[edge_types.CLAIMS_COLUMN]}
        self.assertEqual(read, claims)
        self.assertEqual(row["source_id"], "chunk-1")

if __name__ == "__main__":
    unittest.main()
