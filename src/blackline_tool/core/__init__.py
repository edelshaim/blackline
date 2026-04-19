from .models import (
    Token,
    CompareOptions,
    RedlineParagraph,
    DocumentBlock,
    RedlineSection,
    ReportSummary,
    RedlineReport,
)
from .diff import (
    tokenize_words,
    diff_words,
    normalize_token,
    normalize_text,
    paragraph_compare_key,
    substantive_key,
    block_alignment_key,
    tokens_equivalent_for_strict,
)
from .utils import (
    options_for_profile,
    INSERT_HEX,
    DELETE_HEX,
    MOVE_HEX,
    active_rule_labels,
    report_profile_summary,
)
from .engine import (
    generate_report,
    load_document_blocks,
    load_text,
    build_report_from_blocks,
    compare_paragraphs_with_options,
    compare_paragraphs,
    compare_paragraphs_strict,
    compare_paragraphs as _compare_paragraphs,
)
from .renderers import (
    write_html_report,
    write_docx_report,
    write_pdf_report,
    write_json_report,
    _render_pdf_tokens,
)
from .docx_engine import (
    write_docx_blackline_with_formatting,
    _WordBlock,
    Document,
    DocxDocumentType,
    DocxParagraph,
    DocxTable,
    CT_P,
    CT_Tbl,
    _paragraph_layout_signature,
    _rewrite_paragraph_with_tokens,
    _iter_docx_block_items,
)
