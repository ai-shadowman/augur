"""What the deterministic join cannot do, asserted rather than measured.

The Phase 7 join measurement was taken on one corpus with one model, and read
as a whole it invites the question of whether a different model would overturn
it. Two thirds of it would not, because two of its three claims are properties
of ``ground_symbols`` rather than observations about celery 2.5.5:

* it re-files a symbol only onto a module the corpus says declares it, so a
  re-file cannot introduce a misattribution, only remove one;
* it drops and renames rows and never appends one, so it cannot recover a
  symbol the model failed to emit in the first place.

The second is why the header's coverage advantage survived the join on both
arms while its precision advantage did not, and that asymmetry is structural.
Pinning both here means a second model has to answer the one claim that is
genuinely about the model, which is whether precision converges.

``_corrected_owner`` already states its four reasons to decline in prose. The
cases below are the adversarial population those reasons exist for: ambiguous
ownership, a name declared nowhere, a bare name with no owner at all, and a
correction landing on a title the model also emitted correctly.
"""

import unittest

from provenance import declarations, graph
from provenance.corpus import dotted_key
from provenance.grounding_report import GroundingReport
from tests.test_provenance_graph import document_frame, edge_frame, entity_frame

#: ``shared`` is deliberately declared twice. One indexed module declaring a
#: name is evidence; two is the ``..ABSTRACT`` case one layer down, where the
#: right answer is not determined and a guess would be the partial-evidence
#: failure in miniature.
DOCUMENTS = [
    (
        "celery/worker/state.py",
        "def task_accepted(): pass\n\nclass LocalCache:\n    def get(self): pass\n\n"
        "def shared(): pass\n",
    ),
    ("celery/app/base.py", "class App:\n    def config_from_object(self): pass\n"),
    ("celery/app/amqp.py", "def shared(): pass\n"),
]

#: One row per reason the join might move a symbol, including the two reasons
#: it must not.
POPULATION = [
    # Real symbol, wrong owner, exactly one indexed module declares it.
    ("CELERY.APP.BASE.TASK_ACCEPTED", "FUNCTION", "Marks a task accepted."),
    # Already right.
    ("CELERY.WORKER.STATE.LOCALCACHE", "CLASS", "A bounded cache."),
    # Real symbol, wrong owner, two indexed modules declare it.
    ("CELERY.APP.BASE.SHARED", "FUNCTION", "Something two modules define."),
    # Declared nowhere in the corpus: invention, and the title is the evidence.
    ("CELERY.NOWHERE.MADE_UP", "FUNCTION", "A name the model assembled."),
    # No owner in the name at all.
    ("TASK_ACCEPTED", "FUNCTION", "Bare, unqualified."),
    # The correct spelling of row one, which the model also emitted.
    ("CELERY.WORKER.STATE.TASK_ACCEPTED", "FUNCTION", "Marks a task accepted."),
]


def misattributed(titles, corpus_symbols, corpus_index) -> list[str]:
    """Titles naming a real symbol under a module that does not declare it.

    The same split ``verify`` reports, computed here from the same two
    inventories ``ground_symbols`` consults, so the before and after counts
    are commensurable.
    """
    out = []
    for title in titles:
        key = dotted_key(str(title))
        if "." not in key or declarations.declared_in(key, corpus_symbols):
            continue
        if declarations.attribution_of(key, corpus_index) is not None:
            out.append(str(title))
    return out


class TestTheJoinCannotMakeAttributionWorse(unittest.TestCase):
    """953 to 73 was a measurement. That it never moves the other way is not."""

    def setUp(self):
        self.documents = document_frame(DOCUMENTS)
        self.corpus_symbols = declarations.scan_corpus(self.documents)
        self.corpus_index = declarations.owners_of(self.corpus_symbols)
        self.report = GroundingReport()
        self.entities, _ = graph.ground_symbols(
            entity_frame(POPULATION), edge_frame(()), self.report, self.documents, None,
        )

    def test_every_refiled_symbol_lands_on_a_module_that_declares_it(self):
        self.assertTrue(self.report.symbols_refiled, "no re-file happened; the case is vacuous")
        for old, new in self.report.symbols_refiled:
            self.assertIsNotNone(
                declarations.declared_in(dotted_key(new), self.corpus_symbols),
                f"re-filed {old} onto {new}, which the corpus does not declare",
            )

    def test_the_misattributed_population_never_grows(self):
        before = misattributed(
            [row[0] for row in POPULATION], self.corpus_symbols, self.corpus_index,
        )
        after = misattributed(
            self.entities["title"], self.corpus_symbols, self.corpus_index,
        )
        self.assertTrue(before, "nothing was misattributed; the case is vacuous")
        self.assertLessEqual(len(after), len(before), f"{before} became {after}")
        # What survives is exactly what the corpus could not resolve. A join
        # that fixed this one would be guessing between two declaring modules.
        self.assertEqual(after, self.report.symbols_ambiguous)

    def test_an_ambiguous_owner_is_recorded_and_left_alone(self):
        self.assertEqual(self.report.symbols_ambiguous, ["CELERY.APP.BASE.SHARED"])
        self.assertIn("CELERY.APP.BASE.SHARED", set(self.entities["title"]))

    def test_a_name_the_corpus_cannot_vouch_for_keeps_the_spelling_the_model_wrote(self):
        # Renaming either of these would destroy the evidence the leaked
        # versus invented split downstream reads off the title.
        titles = set(self.entities["title"])
        self.assertIn("CELERY.NOWHERE.MADE_UP", titles)
        self.assertIn("TASK_ACCEPTED", titles)


class TestTheJoinCannotAddCoverage(unittest.TestCase):
    """Why the parser header's extra 188 grounded symbols survived the join.

    The join grounds entities that exist. A symbol the model never emitted has
    no row to correct, so no amount of post-processing recovers it, and a
    header that makes the model emit more of them is buying something the join
    structurally cannot.
    """

    def test_the_symbol_population_never_grows(self):
        documents = document_frame(DOCUMENTS)
        entities, _ = graph.ground_symbols(
            entity_frame(POPULATION), edge_frame(()), GroundingReport(), documents, None,
        )
        self.assertLessEqual(len(entities), len(POPULATION))

    def test_no_output_title_is_new_to_both_the_input_and_the_corpus(self):
        documents = document_frame(DOCUMENTS)
        corpus_symbols = declarations.scan_corpus(documents)
        entities, _ = graph.ground_symbols(
            entity_frame(POPULATION), edge_frame(()), GroundingReport(), documents, None,
        )
        offered = {row[0] for row in POPULATION}
        for title in entities["title"]:
            if str(title) in offered:
                continue
            self.assertIsNotNone(
                declarations.declared_in(dotted_key(str(title)), corpus_symbols),
                f"{title} is in neither the model's output nor the corpus",
            )

    def test_a_symbol_the_corpus_declares_but_the_model_missed_is_not_added(self):
        # celery/app/base.py declares App.config_from_object and nothing in
        # POPULATION names it. The join sees the declaration and still does
        # not write the row, which is the whole of the coverage asymmetry.
        documents = document_frame(DOCUMENTS)
        entities, _ = graph.ground_symbols(
            entity_frame(POPULATION), edge_frame(()), GroundingReport(), documents, None,
        )
        folded = {str(t).upper() for t in entities["title"]}
        self.assertIn(
            "app.config_from_object",
            declarations.scan_corpus(documents)["celery.app.base"],
            "the corpus must declare it, or this test proves nothing",
        )
        self.assertNotIn("CELERY.APP.BASE.APP.CONFIG_FROM_OBJECT", folded)


if __name__ == "__main__":
    unittest.main()
