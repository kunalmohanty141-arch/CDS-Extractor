"""DDL generation — F-19 … F-22, F-25.

Zero SAP dependency: metadata and AST in, DDL text out. Nothing here writes to
a system; the write pipeline (F-27 … F-32) is Stage 4 and sits above this.
"""

from cdcforge.generator.emit import (
    quote,
    render_analytics_block,
    render_cdc_mapping,
    render_element_list,
    render_header,
)
from cdcforge.generator.mapping import (
    MappingProposal,
    TableDecision,
    build_cdc_mapping,
    mandatory_elements,
)
from cdcforge.generator.naming import (
    MAX_CDS_NAME,
    NameCheck,
    NamePreview,
    NamingConvention,
    check_name,
    preview_names,
)
from cdcforge.generator.wrapper import generate_wrapper
from cdcforge.generator.ztable import GeneratedObject, generate_view_for_table

__all__ = [
    "MAX_CDS_NAME",
    "GeneratedObject",
    "MappingProposal",
    "NameCheck",
    "NamePreview",
    "NamingConvention",
    "TableDecision",
    "build_cdc_mapping",
    "check_name",
    "generate_view_for_table",
    "generate_wrapper",
    "mandatory_elements",
    "preview_names",
    "quote",
    "render_analytics_block",
    "render_cdc_mapping",
    "render_element_list",
    "render_header",
]
