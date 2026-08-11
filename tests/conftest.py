from __future__ import annotations

import json
from pathlib import Path

import pytest

from cdcforge.metadata import MockMetadataSource
from cdcforge.model import Assessment
from cdcforge.parsing.ddl import parse_ddl
from cdcforge.rules import ValidationContext, validate_object, validate_source

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def metadata() -> MockMetadataSource:
    return MockMetadataSource(FIXTURES)


@pytest.fixture(scope="session")
def expected() -> dict:
    raw = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


@pytest.fixture
def assess(metadata):
    """Validate a fixture view by name."""

    def _assess(name: str, **kwargs) -> Assessment:
        return validate_object(name, metadata, **kwargs)

    return _assess


@pytest.fixture
def assess_ddl(metadata):
    """Validate inline DDL against the fixture metadata."""

    def _assess(source: str, name: str = "ZI_TEST", **kwargs) -> Assessment:
        kwargs.setdefault("metadata", metadata)
        return validate_source(source, name=name, **kwargs)

    return _assess


@pytest.fixture
def context(metadata):
    """Build a ValidationContext for a fixture view."""

    def _context(name: str, **kwargs) -> ValidationContext:
        source = metadata.get_view_source(name)
        assert source is not None, f"no fixture named {name}"
        kwargs.setdefault("metadata", metadata)
        return ValidationContext(view=parse_ddl(source, name_hint=name), **kwargs)

    return _context
