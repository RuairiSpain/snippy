from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from eu_ai_action.collector.fixtures import FixtureSource
from eu_ai_action.documents import load_doc_types
from eu_ai_action.documents.narrative import DerivedNarrative
from eu_ai_action.rules import load_rules
from eu_ai_action.services import Pipeline
from eu_ai_action.settings import Settings
from eu_ai_action.store import LocalFileStore, MemoryStore

FIXTURES = str(Path(__file__).parent / "fixtures")
TENANT = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None, use_fixtures=True, fixtures_dir=FIXTURES, files_dir=str(tmp_path / "files"),
        require_counsel_review_for_release=False, auth_disabled=True, use_memory_store=True,
    )


@pytest.fixture
def pipeline(settings, tmp_path) -> Pipeline:
    return Pipeline(
        settings, MemoryStore(), LocalFileStore(settings.files_dir), [FixtureSource(FIXTURES)],
        load_rules(settings.rules_dir), load_doc_types(settings.document_types_file), DerivedNarrative(),
        today=date(2026, 11, 8),
    )
