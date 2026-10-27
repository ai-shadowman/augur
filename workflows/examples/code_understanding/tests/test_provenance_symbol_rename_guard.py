"""The symbol layer must not refile a title the checkout already grounds.

``celery/bin/base.py`` binds ``Option`` by ``from optparse import make_option
as Option``. The model wrote ``celery.bin.base.Option`` correctly, and the
rename moved it onto ``celery.app.defaults.Option``, an unrelated class,
because the declared inventory holds no imported names. Kept out of
``test_provenance_graph.py``, which is not to grow.
"""

from __future__ import annotations

import unittest

from provenance import declarations
from provenance.edge_evidence import EdgeEvidence
from provenance.graph import _corrected_owner

CORPUS = {
    "celery.app.defaults": ["option"],
    "celery.bin.base": ["command"],
    "celery.utils.timeutils": ["timedelta_seconds"],
    "pkg.helpers": ["utils"],
}
CHECKOUT = CORPUS

#: ``celery.bin.base`` binds ``Option`` from outside the checkout;
#: ``celery.schedules`` re-exports ``timedelta_seconds`` from where it lives.
EDGES = EdgeEvidence([], {
    "celery.app.defaults.option": ["celery/app/defaults.py"],
    "celery.bin.base.option": ["celery/bin/base.py"],
    "celery.utils.timeutils.timedelta_seconds": ["celery/utils/timeutils.py"],
    "celery.schedules.timedelta_seconds": ["celery/schedules.py", "celery/utils/timeutils.py"],
    "pkg.utils": ["pkg/utils.py"],
    "pkg.helpers.utils": ["pkg/helpers.py"],
})


class TestKnownTitlesStay(unittest.TestCase):
    def test_cases(self):
        index = declarations.owners_of(CORPUS)
        cases = [
            # (key, edge evidence or "relative", expected)
            ("celery.bin.base.option", None, "CELERY.APP.DEFAULTS.OPTION"),
            ("celery.bin.base.option", EDGES, None),
            # A title the checkout does not ground is still corrected.
            ("celery.bin.other.option", EDGES, "CELERY.APP.DEFAULTS.OPTION"),
            # A relative title lands only on a module ending in what it spells.
            ("base.option", "relative", None),
            ("defaults.option", "relative", "CELERY.APP.DEFAULTS.OPTION"),
            # The model's ". _WIN.KILL_PROCESSTREE" folds with its stray space.
            (" timeutils.timedelta_seconds", "relative", "CELERY.UTILS.TIMEUTILS.TIMEDELTA_SECONDS"),
            # A module's own key is a file, not a bound name; the guard stands aside.
            ("pkg.utils", EDGES, "PKG.HELPERS.UTILS"),
            # A re-export collapses onto its declaring module: one object.
            ("celery.schedules.timedelta_seconds", EDGES,
             "CELERY.UTILS.TIMEUTILS.TIMEDELTA_SECONDS"),
        ]
        for key, edges, want in cases:
            relative = edges == "relative"
            edges = None if relative else edges
            with self.subTest(key=key, edges=edges is not None, relative=relative):
                got = _corrected_owner(key, CORPUS, index, CHECKOUT, edges, relative=relative)
                self.assertEqual(got, want)



class TestNoRefileOntoAnAlias(unittest.TestCase):
    """A class member refiled by its last segment onto another module's binding.

    ``celery.registry.tasks = TaskRegistry()`` is not ``BaseApp.tasks``. The
    path in the class's place may instead name the landing's module, as in
    ``celery.beat`` + ``app.app_or_default``, and then the refile stands.
    """

    CORPUS = {
        "celery.app.base": ["baseapp", "baseapp.tasks"],
        "celery.registry": ["taskregistry", "tasks"],
        "celery.app.task": ["basetask", "extract_exec_options"],
        "celery.app": ["app_or_default"],
        "celery.beat": ["scheduler"],
        "celery.platforms": ["detach", "setuid"],
    }
    BINDINGS = {"celery.registry.tasks", "celery.app.task.extract_exec_options", "celery.app.app_or_default"}

    def test_cases(self):
        index = declarations.owners_of(self.CORPUS)
        cases = [
            ("celery.app.base.celery.tasks", self.BINDINGS, None),
            ("celery.app.base.celery.tasks", set(), "CELERY.REGISTRY.TASKS"),
            ("celery.app.task.task.extract_exec_options", self.BINDINGS, "CELERY.APP.TASK.EXTRACT_EXEC_OPTIONS"),
            ("celery.beat.app.app_or_default", self.BINDINGS, "CELERY.APP.APP_OR_DEFAULT"),
            # A function is not an alias; nesting it under a sibling is undone.
            ("celery.platforms.detach.setuid", self.BINDINGS, "CELERY.PLATFORMS.SETUID"),
            # No path between module and symbol: an ordinary misattribution.
            ("celery.beat.tasks", self.BINDINGS, "CELERY.REGISTRY.TASKS"),
        ]
        for key, bindings, want in cases:
            with self.subTest(key=key, bindings=bool(bindings)):
                got = _corrected_owner(key, self.CORPUS, index, self.CORPUS, bindings=bindings)
                self.assertEqual(got, want)


class TestUniqueOnlyAcrossTheCheckout(unittest.TestCase):
    """A namesake unique in the indexed subset is not unique evidence.

    The cluster smoke indexed 18 documents and refiled ``abstract.Component``
    onto ``celery.worker.consumer.Component``, the only ``Component`` among
    them; the real base is ``celery.abstract.Component``, not indexed.
    """

    CORPUS = {
        "celery.worker.consumer": ["component", "consumer"],
        "celery.app.annotations": ["prepare"],
    }
    CHECKOUT = {
        **CORPUS,
        "celery.abstract": ["component"],
        "celery.routes": ["prepare"],
    }

    def test_cases(self):
        index = declarations.owners_of(self.CORPUS)
        cases = [
            # (key, relative, expected): "" is recorded as ambiguous.
            ("abstract.component", False, ""),
            ("celery.worker.abstract.component", False, ""),
            ("celery.app.amqp._routes.prepare", False, ""),
            # A relative title's rival must end with what it spells, as the
            # landing must; ``celery.routes`` does not end with ``annotations``.
            ("annotations.prepare", True, "CELERY.APP.ANNOTATIONS.PREPARE"),
            # No rival in the checkout: the refile stands.
            ("celery.worker.panel.consumer", False, "CELERY.WORKER.CONSUMER.CONSUMER"),
        ]
        for key, relative, want in cases:
            with self.subTest(key=key):
                got = _corrected_owner(key, self.CORPUS, index, self.CHECKOUT, relative=relative)
                self.assertEqual(got, want)

    def test_a_longer_namesake_outside_the_corpus_is_a_rival(self):
        corpus = {"m1": ["c"]}
        checkout = {**corpus, "z": ["b", "b.c"]}
        got = _corrected_owner("a.b.c", corpus, declarations.owners_of(corpus), checkout)
        self.assertEqual(got, "")

    def test_import_evidence_settles_what_a_rival_would_leave_open(self):
        corpus = {"celery.app.abstract": ["configurated"], "celery.worker": ["workcontroller"]}
        checkout = {**corpus, "celery.tests.test_worker": ["configurated"]}
        index = declarations.owners_of(corpus)
        cases = [
            # ``celery.worker`` binds ``configurated`` from ``celery.app.abstract``.
            (["celery/worker/__init__.py", "celery/app/abstract.py"], "CELERY.APP.ABSTRACT.CONFIGURATED"),
            # A binding that leads elsewhere rejects the landing outright,
            # rather than recording it as ambiguous.
            (["celery/worker/__init__.py", "other/place.py"], None),
            # No evidence: the test-module rival leaves it open.
            (None, ""),
        ]
        for files, want in cases:
            edges = EdgeEvidence([], {
                "celery.app.abstract.configurated": ["celery/app/abstract.py"],
                **({"celery.worker.configurated": files} if files else {}),
            })
            with self.subTest(files=files):
                got = _corrected_owner("celery.worker.configurated", corpus, index, checkout, edges)
                self.assertEqual(got, want)


if __name__ == "__main__":
    unittest.main()
