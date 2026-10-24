"""Deterministic ground truth for the code-understanding pipeline.

The indexing pipeline asks a language model for facts it does not need to ask
for. Repository URLs, declared dependency constraints and base images are all
recoverable from the checkout, and a model that is asked for them can drop or
invent them. This package extracts those facts without a model (``facts``,
``manifests``, ``sbom``), enriches them with upstream health data (``health``),
and checks a finished index against them (``verify``).

The three layers a migration analysis must keep separate, OS, package and
application, are modelled explicitly in :class:`models.Layer`.
"""

from .facts import collect, read, write
from .models import (
    BaseImage,
    Finding,
    FindingKind,
    Layer,
    Package,
    RepoFacts,
    VerificationReport,
)
from .verify import verify_index

__all__ = [
    "BaseImage",
    "Finding",
    "FindingKind",
    "Layer",
    "Package",
    "RepoFacts",
    "VerificationReport",
    "collect",
    "read",
    "verify_index",
    "write",
]
