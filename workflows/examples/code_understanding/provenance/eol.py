"""End-of-life status for the runtime stack, read from a dated table.

The migration report used to ask a model to "check each version for known CVEs
or end-of-life status". Across five analysis replicates it cited no advisory and
made 5 to 12 end-of-life claims per run, all recalled. A lifecycle date is a
published fact with a source, so this module looks it up instead.

The table is ``assets/eol/lifecycles.json``. It is generated, never edited by
hand: ``python -m provenance eol-table`` fetches each product's cycles from
endoflife.date and records the retrieval date beside them, and every line the
report prints cites that date. A stale table is therefore visibly stale rather
than silently wrong.

Coverage is runtimes and frameworks, the components the ``Runtime:`` header
names. Each product is keyed by the names a header may use for it. Java has no
single lifecycle, since support depends on who ships the JDK; the table uses
the Red Hat build of OpenJDK, because a RHEL migration lands on it, and the
report names that vendor rather than implying the date is universal.

Every component gets one :class:`Status`. A name the table does not hold, or a
version it has no cycle for, says so; neither is ever reported as supported.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

#: The committed table, beside the other static assets.
TABLE_PATH = Path(__file__).resolve().parent.parent / "assets" / "eol" / "lifecycles.json"

SOURCE = "https://endoflife.date/api/{product}.json"

FETCH_TIMEOUT_SECONDS = 30

#: Table key to the endoflife.date product, the lifecycle named in the report,
#: the header names that mean it, and version spellings that mean a cycle.
PRODUCTS: dict[str, dict[str, Any]] = {
    "python": {"product": "python", "label": "Python (upstream)",
               "names": ["python", "cpython"]},
    "ruby": {"product": "ruby", "label": "Ruby (upstream)", "names": ["ruby"]},
    "java": {"product": "redhat-build-of-openjdk", "label": "Red Hat build of OpenJDK",
             "names": ["java", "openjdk", "jdk", "jre"],
             "versions": {"1.6": "6", "1.7": "7", "1.8": "8"}},
    "nodejs": {"product": "nodejs", "label": "Node.js (upstream)",
               "names": ["node", "nodejs", "node.js"]},
    "django": {"product": "django", "label": "Django (upstream)", "names": ["django"]},
    "rails": {"product": "rails", "label": "Ruby on Rails (upstream)",
              "names": ["rails", "ruby on rails"]},
    "spring-framework": {"product": "spring-framework", "label": "Spring Framework (upstream)",
                         "names": ["spring", "spring framework"]},
}


class Status(StrEnum):
    ENDED = "ended"
    SUPPORTED = "supported"
    NO_DATE = "no end date announced"
    UNKNOWN_VERSION = "version not in table"
    UNKNOWN_COMPONENT = "not in table"


@dataclass(frozen=True)
class Assessment:
    component: str
    status: Status
    lifecycle: str | None = None
    cycle: str | None = None
    #: ISO date, or ``None`` where the source gives none.
    eol: str | None = None


@dataclass(frozen=True)
class Table:
    retrieved: str
    source: str
    products: dict[str, dict[str, Any]]

    def assess(self, component: str, today: dt.date) -> Assessment:
        """Status for one ``Name Version`` component of a runtime header."""
        name, version = _split(component)
        key = next((k for k, p in self.products.items() if name in p["names"]), None)
        if key is None:
            return Assessment(component, Status.UNKNOWN_COMPONENT)
        product = self.products[key]
        version = product.get("versions", {}).get(version, version)
        cycle = _cycle_for(version, product["cycles"])
        if cycle is None:
            return Assessment(component, Status.UNKNOWN_VERSION, product["label"])
        eol = product["cycles"][cycle]
        base = {"component": component, "lifecycle": product["label"], "cycle": cycle}
        if eol is True:
            return Assessment(status=Status.ENDED, **base)
        if eol is False or eol is None:
            return Assessment(status=Status.NO_DATE, **base)
        ended = dt.date.fromisoformat(eol) <= today
        return Assessment(status=Status.ENDED if ended else Status.SUPPORTED, eol=eol, **base)


def _split(component: str) -> tuple[str, str]:
    """``"Python 2.7"`` to ``("python", "2.7")``; the version is the last word."""
    words = component.strip().rsplit(None, 1)
    if len(words) < 2:
        return component.strip().lower(), ""
    return words[0].lower(), words[1].lstrip("vV")


def _cycle_for(version: str, cycles: dict[str, Any]) -> str | None:
    """The longest dotted prefix of ``version`` that names a cycle.

    ``2.7.18`` finds ``2.7``. A version that is not dotted numbers, such as the
    ``2.7=end`` a misparsed Ruby header produces, finds nothing.
    """
    if not re.fullmatch(r"\d+(\.\d+)*", version):
        return None
    parts = version.split(".")
    for n in range(len(parts), 0, -1):
        candidate = ".".join(parts[:n])
        if candidate in cycles:
            return candidate
    return None


def components(runtime: str | None) -> list[str]:
    """The components of a ``Runtime:`` header, which joins them with ``/``."""
    return [c.strip() for c in (runtime or "").split("/") if c.strip()]


def load(path: Path = TABLE_PATH) -> Table:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Table(retrieved=data["retrieved"], source=data["source"], products=data["products"])


def fetch(today: dt.date) -> dict[str, Any]:
    """Build the table from endoflife.date. Network; run by hand to refresh."""
    products = {}
    for key, spec in PRODUCTS.items():
        url = SOURCE.format(product=spec["product"])
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:
            cycles = json.load(response)
        products[key] = {**spec, "cycles": {str(c["cycle"]): c["eol"] for c in cycles}}
    return {"retrieved": today.isoformat(), "source": SOURCE, "products": products}
