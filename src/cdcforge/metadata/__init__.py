"""Metadata access — abstract interface plus the offline implementations."""

from cdcforge.metadata.base import MetadataSource, NullMetadataSource
from cdcforge.metadata.mock import FixtureError, MockMetadataSource
from cdcforge.metadata.types import (
    ApiState,
    FieldMeta,
    ObjectMeta,
    Owner,
    TableClass,
    TableMeta,
    derive_owner,
)

__all__ = [
    "ApiState",
    "FieldMeta",
    "FixtureError",
    "MetadataSource",
    "MockMetadataSource",
    "NullMetadataSource",
    "ObjectMeta",
    "Owner",
    "TableClass",
    "TableMeta",
    "derive_owner",
]
