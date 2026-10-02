from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from senso_lens.ingest.pipeline import init_index
from senso_lens.store.db import open_store
from tests.fixture_repo import build


@pytest.fixture(scope="session")
def fixture_repo(tmp_path_factory) -> Path:
    """The scripted repository (built once per session, read-only for tests)."""
    root = tmp_path_factory.mktemp("fixture") / "repo"
    build(root)
    return root


@pytest.fixture(scope="session")
def indexed_repo(fixture_repo: Path) -> Path:
    """A copy of the fixture with `.senso/index.db` built at HEAD."""
    root = fixture_repo.parent / "indexed"
    shutil.copytree(fixture_repo, root)
    init_index(root, scale="monthly")
    return root


@pytest.fixture()
def repo_copy(indexed_repo: Path, tmp_path: Path) -> Path:
    """A fresh writable copy of the indexed repository for tests that commit or record."""
    dst = tmp_path / "repo"
    shutil.copytree(indexed_repo, dst)
    return dst


@pytest.fixture()
def store(indexed_repo: Path):
    s = open_store(indexed_repo)
    assert s is not None
    yield s
    s.close()
