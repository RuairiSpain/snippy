"""Builds the object graph from settings. Cached per process."""
from __future__ import annotations

from functools import lru_cache

from .api import Api
from .collector import make_source
from .documents import load_doc_types
from .documents.narrative import make_narrative
from .rules import load_rules
from .services import Pipeline
from .settings import Settings, get_settings
from .store import make_file_store, make_store


def build_pipeline(s: Settings) -> Pipeline:
    return Pipeline(
        s, make_store(s), make_file_store(s), make_source(s), load_rules(s.rules_dir), load_doc_types(s.document_types_file), make_narrative(s)
    )


@lru_cache
def get_pipeline() -> Pipeline:
    return build_pipeline(get_settings())


@lru_cache
def get_api() -> Api:
    return Api(get_settings(), get_pipeline())
