"""Give the provenance tests real pandas without disturbing anything else.

``test_duration_tracker`` installs MagicMock entries in ``sys.modules`` for
pandas and friends at import time so it can run where they are not installed.
Collection order means that stub is in place by the time the provenance
modules load, and ``verify`` genuinely needs a real DataFrame.

Swapping globally is not an option: several existing tests only pass while
pandas is a mock. So the swap is scoped to the provenance modules and undone
afterwards, which leaves every other test seeing exactly what it saw before.

Provenance test modules must therefore not build a DataFrame at import time,
only inside a test or its setUp.

The ``test_provenance`` prefix is a proxy for "this module needs a real
DataFrame", and a module can be both: the non-Python header tests render
through ``generate_code_comment`` and then ground the result, so they belong
with data generation by name and need real pandas by behaviour. Setting
``REAL_DATAFRAMES = True`` at module level asks for the swap directly, which
is what the prefix was standing in for.
"""

import sys
from unittest.mock import MagicMock

import pytest

STUBBABLE = ("pandas", "pandas.core", "pyarrow")


@pytest.fixture(autouse=True, scope="module")
def real_dataframes(request):
    name = request.module.__name__.rpartition(".")[2]
    wanted = name.startswith("test_provenance") or getattr(
        request.module, "REAL_DATAFRAMES", False
    )
    if not wanted:
        yield
        return

    stubs = {m: sys.modules[m] for m in STUBBABLE if isinstance(sys.modules.get(m), MagicMock)}
    for module in stubs:
        del sys.modules[module]
    try:
        import pandas  # noqa: F401  real, now that the stub is out of the way

        yield
    finally:
        for module, stub in stubs.items():
            sys.modules[module] = stub
