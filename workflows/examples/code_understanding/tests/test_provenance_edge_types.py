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
from provenance.edge_types import (
    AUGUR_WRITTEN,
    BOTH,
    PRECEDENCE,
    SOURCE,
    TARGET,
    UNKNOWN,
    UNTYPED,
    VOCABULARY,
    subject_of,
    type_of,
    types_in,
    with_subject,
    with_type,
)

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
    # AUGUR-written rows carry their own type, and a model claim outranks it.
    ("CELERY/WORKER/STATE.PY BELONGS_TO the CELERY-V2-5-5 repository", {"BELONGS_TO"}, "BELONGS_TO"),
    ("PKG.MOD.TOP DEFINED_IN PKG/MOD.PY", {"DEFINED_IN"}, "DEFINED_IN"),
    ("PKG.MOD.TOP DEFINED_IN PKG/MOD.PY\nPKG.MOD.TOP CALLS PKG/MOD.PY", {"CALLS", "DEFINED_IN"}, "CALLS"),
    ("", set(), UNTYPED),
    (None, set(), UNTYPED),
]


class TestTypes(unittest.TestCase):
    def test_real_rows(self):
        for description, named, column in CASES:
            with self.subTest(description=description):
                self.assertEqual(types_in(description), named)
                self.assertEqual(type_of(description), column)

    def test_vocabulary_is_the_prompts_seven_augurs_two_and_untyped(self):
        self.assertEqual(len(PRECEDENCE), 7)
        self.assertEqual(VOCABULARY, set(PRECEDENCE) | set(AUGUR_WRITTEN) | {UNTYPED})

    def test_oracle_checkable_types_outrank_the_rest(self):
        # provenance.edges settles INHERITS and IMPORTS; nothing settles the
        # other in-repository types, so a row naming both is checkable.
        for checkable in ("INHERITS", "IMPORTS"):
            for other in ("CALLS", "USES", "DEPENDS_ON"):
                self.assertLess(PRECEDENCE.index(checkable), PRECEDENCE.index(other))


SUBJECTS = [
    # (source column, target column, description, the column the subject names).
    # Verbatim from the same three indexes unless marked constructed.
    ("CELERY/APPS/WORKER.PY", "CELERY.PLATFORMS",
     "celery/apps/worker.py IMPORTS the celery.platforms module.", SOURCE),
    # graphrag's insertion order put the subject in the target column.
    ("CMD", "CELERY.BIN.CAMQADM.AMQSHELL",
     "celery.bin.camqadm.AMQShell INHERITS from cmd (module)", TARGET),
    # A bare subject for a qualified column.
    ("CELERY/APP/SHELL.SHELL.RUN", "CELERY/APP/SHELL.SHELL.INVOKE_DEFAULT_SHELL",
     "run CALLS invoke_default_shell when no specific shell flag is set.", SOURCE),
    # A member call on a bare column decides through the object.
    ("FILEUTILS", "JEKYLL.COMMANDS.NEW.PROCESS",
     "process CALLS FileUtils.mkdir_p to create the target directory", TARGET),
    # The article is skipped before the object.
    ("SRC/MAIN/JAVA/ORG/APACHE/COMMONS/LANG3/CONCURRENT/UNCHECKEDTIMEOUTEXCEPTION.JAVA",
     "JAVA.UTIL.CONCURRENT.TIMEOUTEXCEPTION",
     "src/main/java/... IMPORTS the java.util.concurrent.TimeoutException standard library class.", SOURCE),
    # Lines that agree.
    ("CELERY/APP/SHELL.SHELL.INVOKE_FALLBACK_SHELL", "CODE.INTERACT",
     "invoke_fallback_shell CALLS code.interact to start a basic Python REPL.\n"
     "invoke_fallback_shell CALLS code.interact to start a basic REPL.", SOURCE),
    # Constructed: lines that disagree.
    ("PKG.A", "PKG.B", "PKG.A CALLS PKG.B to start\nPKG.B CALLS PKG.A back", BOTH),
    # Two columns folding to one key say nothing.
    ("LAZYINITIALIZER.BUILDER", "LAZYINITIALIZER.BUILDER",
     "LazyInitializer.builder CALLS LazyInitializer.Builder to create a new builder instance.", UNKNOWN),
    # Constructed: a bare name fitting both columns is not a unique fit.
    ("PKG.A.RUN", "PKG.B.RUN", "run CALLS run", UNKNOWN),
    # No token, no reading.
    ("CELERY/EXCEPTIONS.PY", "CELERY.EXCEPTIONS.CPENDINGDEPRECATIONWARNING",
     "celery.exceptions.CPendingDeprecationWarning is defined in celery/exceptions.py", UNKNOWN),
    ("PKG.A", "PKG.B", None, UNKNOWN),
    # What provenance.containment writes.
    ("PKG.MOD.TOP", "PKG/MOD.PY", "PKG.MOD.TOP DEFINED_IN PKG/MOD.PY", SOURCE),
]


class TestSubject(unittest.TestCase):
    def test_real_rows(self):
        for source, target, description, side in SUBJECTS:
            with self.subTest(description=description):
                self.assertEqual(subject_of(source, target, description), side)

    def test_with_subject_adds_the_column(self):
        frame = {
            "source": [r[0] for r in SUBJECTS],
            "target": [r[1] for r in SUBJECTS],
            "description": [r[2] for r in SUBJECTS],
        }
        out = with_subject(frame)
        self.assertNotIn(edge_types.SUBJECT_COLUMN, frame)
        self.assertEqual(out[edge_types.SUBJECT_COLUMN], [r[3] for r in SUBJECTS])


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
