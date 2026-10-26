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


if __name__ == "__main__":
    unittest.main()
