"""
Pytest session-scoped configuration and shared fixtures for Ghost Data Still Exists.
Precomputes and caches EvidenceLoader, IdentityResolver, and DeletionAuditor
to speed up test execution from ~57s to under 5s.
"""

import os
import sys
import pytest
from pathlib import Path

# Ensure code dir is on sys.path
code_dir = Path(__file__).resolve().parent.parent
if str(code_dir) not in sys.path:
    sys.path.insert(0, str(code_dir))

from identity import IdentityResolver
from evidence_loader import EvidenceLoader
from auditor import DeletionAuditor


@pytest.fixture(scope="session")
def dataset_dir():
    """Finds and returns the root dataset directory."""
    candidates = [
        code_dir.parent / "dataset",
        Path("dataset").resolve(),
        Path("../dataset").resolve(),
        Path("../../dataset").resolve(),
    ]
    for c in candidates:
        if c.exists() and (c / "evidence_files.csv").exists():
            return str(c)
    return str(code_dir.parent / "dataset")


@pytest.fixture(scope="session", autouse=True)
def precompute_dataset_caches(shared_evidence_loader):
    """Precompute and cache parsed SQLite tables and log tokens in memory."""
    return shared_evidence_loader


@pytest.fixture(scope="session")
def shared_identity_resolver(dataset_dir):
    """Session-scoped IdentityResolver instance."""
    return IdentityResolver(dataset_dir)


@pytest.fixture(scope="session")
def identity_resolver(shared_identity_resolver):
    """Alias for shared_identity_resolver."""
    return shared_identity_resolver


@pytest.fixture(scope="session")
def resolver(shared_identity_resolver):
    """Convenience alias for shared_identity_resolver."""
    return shared_identity_resolver


@pytest.fixture(scope="session")
def shared_evidence_loader(dataset_dir):
    """Session-scoped EvidenceLoader instance."""
    return EvidenceLoader(dataset_dir)


@pytest.fixture(scope="session")
def evidence_loader(shared_evidence_loader):
    """Alias for shared_evidence_loader."""
    return shared_evidence_loader


@pytest.fixture(scope="session")
def loader(shared_evidence_loader):
    """Convenience alias for shared_evidence_loader."""
    return shared_evidence_loader


@pytest.fixture(scope="session")
def shared_auditor(dataset_dir, shared_identity_resolver, shared_evidence_loader):
    """Session-scoped DeletionAuditor instance reusing shared loader and resolver."""
    return DeletionAuditor(
        dataset_dir=dataset_dir,
        resolver=shared_identity_resolver,
        loader=shared_evidence_loader,
    )


@pytest.fixture(scope="session")
def auditor(shared_auditor):
    """Alias for shared_auditor."""
    return shared_auditor
