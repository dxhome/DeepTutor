"""Public question-document import interface.

Document parsing is performed locally by explicit extraction and numbering
rules. This module intentionally has no model-provider dependency.
"""

from .document_import_rules import (
    DOCUMENT_EXTENSIONS,
    MAX_DOCUMENT_BYTES,
    MAX_DOCUMENT_CHARS,
    MAX_DOCUMENT_PAGES,
    parse_document,
    validate_candidate,
)

__all__ = [
    "DOCUMENT_EXTENSIONS",
    "MAX_DOCUMENT_BYTES",
    "MAX_DOCUMENT_CHARS",
    "MAX_DOCUMENT_PAGES",
    "parse_document",
    "validate_candidate",
]
