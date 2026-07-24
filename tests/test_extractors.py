"""Extractors are the app's only untrusted input surface — every one of them
parses a file someone else produced. Fixtures are built in-process rather than
committed as binaries, so the suite stays dependency-light and readable.
"""

import io

import pytest

from app.ingest import extractors


def test_text_file():
    source = extractors.extract_file("note.txt", b"Plain text content.")
    assert source.source_type == "text"
    assert source.text == "Plain text content."
    assert source.raw_bytes == b"Plain text content."  # verbatim, for provenance


def test_markdown_is_treated_as_text():
    assert extractors.extract_file("note.md", b"# Heading").source_type == "text"


def test_csv():
    source = extractors.extract_file("data.csv", b"a,b\n1,2\n")
    assert source.source_type == "csv"
    assert "a,b" in source.text


def test_unsupported_type_is_rejected_clearly():
    with pytest.raises(extractors.ExtractionError, match="Unsupported file type"):
        extractors.extract_file("archive.zip", b"PK\x03\x04")


def test_undecodable_bytes_do_not_crash():
    """Replacement rather than an exception: a mangled encoding shouldn't lose
    the whole source."""
    source = extractors.extract_file("note.txt", b"caf\xff\xfe")
    assert source.text  # decoded with replacement characters


def test_pasted_text():
    source = extractors.extract_pasted("Some pasted notes.")
    assert source.source_type == "pasted_text"
    assert source.filename == "pasted.txt"


def test_pasted_text_is_capped():
    source = extractors.extract_pasted("x" * (extractors.MAX_CHARS + 5000))
    assert len(source.text) == extractors.MAX_CHARS


def test_docx():
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("First paragraph.")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "left"
    table.rows[0].cells[1].text = "right"
    buf = io.BytesIO()
    document.save(buf)

    source = extractors.extract_file("doc.docx", buf.getvalue())

    assert source.source_type == "docx"
    assert "First paragraph." in source.text
    assert "left | right" in source.text  # table cells are flattened, not dropped


def test_pptx_includes_speaker_notes():
    pptx = pytest.importorskip("pptx")
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "Quarterly Review"
    slide.notes_slide.notes_text_frame.text = "Mention the revenue miss."
    buf = io.BytesIO()
    prs.save(buf)

    source = extractors.extract_file("deck.pptx", buf.getvalue())

    assert "Quarterly Review" in source.text
    assert "Mention the revenue miss." in source.text


def test_xlsx_reports_structure_and_samples_rows():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Figures"
    ws.append(["region", "revenue"])
    ws.append(["EMEA", 4200])
    buf = io.BytesIO()
    wb.save(buf)

    source = extractors.extract_file("book.xlsx", buf.getvalue())

    assert source.source_type == "xlsx"
    assert "Sheet: Figures" in source.text
    assert "EMEA | 4200" in source.text


def test_scanned_pdf_raises_a_clear_error():
    """OCR is deferred, so the failure has to name the reason rather than
    silently ingesting an empty page."""
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)

    with pytest.raises(extractors.ExtractionError, match="OCR"):
        extractors.extract_file("scan.pdf", buf.getvalue())
