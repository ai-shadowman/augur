"""Reading a relationship's type back out of its description.

Every description below is verbatim from a ``relationships.parquet`` the
120b model produced: celery 2.5.5 parser full, commons-lang 3.21.0 and jekyll
4.4.1 parser smoke. The two ``CROSS_REPO`` cases are constructed, because no
index in reach has one. The rule they pin was registered before it was
counted; the split it produced is in ``augur-test/corpus/MANIFEST.md`` under
Relationship layer Phase 2.

The multi-line rows are not GraphRAG merging duplicates across chunks. They
are its per-chunk ``nx.Graph`` folding A->B and B->A, or a repeated pair, into
one edge with the descriptions joined on a newline, which is why one row can
name two types.
"""

import unittest

from provenance import edge_types
from provenance.edge_types import PRECEDENCE, UNTYPED, VOCABULARY, type_of, types_in, with_type

CASES = [
    # (description, every type named, the type the column carries)
    ("celery/apps/worker.py IMPORTS the celery.platforms module.",
     {"IMPORTS"}, "IMPORTS"),
    ("timer.Timer.clear CALLS timer.Timer.schedule.clear to remove all pending entries.",
     {"CALLS"}, "CALLS"),
    ("StateStrategyClosed INHERITS AbstractStateStrategy.",
     {"INHERITS"}, "INHERITS"),
    ("MutableTriple.getMiddle DEPENDS_ON org.apache.commons.lang3.tuple.MutableTriple",
     {"DEPENDS_ON"}, "DEPENDS_ON"),
    # Two types in one row: INHERITS outranks USES.
    ("org.apache.commons.lang3.exception.ContextedException INHERITS org.apache.commons.lang3.exception.ExceptionContext\n"
     "org.apache.commons.lang3.exception.ContextedException USES org.apache.commons.lang3.exception.ExceptionContext as a field type",
     {"INHERITS", "USES"}, "INHERITS"),
    ("celery/conf.py USES the .current_app module to access its conf attribute\n"
     "celery/conf.py IMPORTS the .current_app module from the same package",
     {"IMPORTS", "USES"}, "IMPORTS"),
    ("Jekyll.Tags.Link CALLS Liquid::Template.parse to process the relative path template\n"
     "Jekyll.Tags.Link INHERITS from Liquid::Tag provided by the liquid library",
     {"CALLS", "INHERITS"}, "INHERITS"),
    # One type said three times is one type.
    ("create_blank_site CALLS FileUtils.mkdir to create default sub-directories\n"
     "create_blank_site CALLS FileUtils.cp_r to copy template files\n"
     "create_blank_site CALLS FileUtils.chmod_R to set write permissions",
     {"CALLS"}, "CALLS"),
    # Lowercase prose is not the vocabulary.
    ("Supervisor inherits from PoolThread.", set(), UNTYPED),
    ("getCause iterates over method names and calls getCauseUsingMethodName for each "
     "to attempt cause extraction.", set(), UNTYPED),
    # Containment, the relation the vocabulary has no word for.
    ("celery.exceptions.CPendingDeprecationWarning is defined in celery/exceptions.py", set(), UNTYPED),
    ("Jekyll::Commands::Serve.start_up_webrick is defined in Jekyll::Commands::Serve", set(), UNTYPED),
    # Uppercase verbs outside the vocabulary.
    ("celery.security.setup_security RAISES celery.exceptions.ImproperlyConfigured when "
     "required security settings are missing", set(), UNTYPED),
    ("lib/jekyll/drops/site_drop.rb EXTENDS Forwardable to gain delegation methods.", set(), UNTYPED),
    # Constructed: the underscore keeps DEPENDS_ON out of CROSS_REPO_DEPENDS_ON.
    ("svc/a.py CROSS_REPO_DEPENDS_ON lib-b", {"CROSS_REPO_DEPENDS_ON"}, "CROSS_REPO_DEPENDS_ON"),
    ("svc/a.py CROSS_REPO_IMPORTS lib_b.client\nsvc/a.py DEPENDS_ON lib_b",
     {"CROSS_REPO_IMPORTS", "DEPENDS_ON"}, "CROSS_REPO_IMPORTS"),
    # A token inside a longer word is not a type.
    ("RECALLS and REUSES are not CALLS_X or XUSES", set(), UNTYPED),
    ("", set(), UNTYPED),
    (None, set(), UNTYPED),
]


class TestTypes(unittest.TestCase):
    def test_real_rows(self):
        for description, named, column in CASES:
            with self.subTest(description=description):
                self.assertEqual(types_in(description), named)
                self.assertEqual(type_of(description), column)

    def test_vocabulary_is_the_prompts_seven_plus_untyped(self):
        self.assertEqual(len(PRECEDENCE), 7)
        self.assertEqual(VOCABULARY, set(PRECEDENCE) | {UNTYPED})

    def test_oracle_checkable_types_outrank_the_rest(self):
        # provenance.edges settles INHERITS and IMPORTS; nothing settles the
        # other in-repository types, so a row naming both is checkable.
        for checkable in ("INHERITS", "IMPORTS"):
            for other in ("CALLS", "USES", "DEPENDS_ON"):
                self.assertLess(PRECEDENCE.index(checkable), PRECEDENCE.index(other))


class TestWithType(unittest.TestCase):
    def test_adds_a_closed_column_without_touching_the_input(self):
        # A dict of columns stands in for the DataFrame: the suite stubs
        # pandas, and with_type needs only copy() and column access.
        frame = {"description": [d for d, _, _ in CASES]}
        out = with_type(frame)
        self.assertNotIn(edge_types.TYPE_COLUMN, frame)
        self.assertEqual(out[edge_types.TYPE_COLUMN], [c for _, _, c in CASES])
        self.assertTrue(set(out[edge_types.TYPE_COLUMN]) <= VOCABULARY)


if __name__ == "__main__":
    unittest.main()
