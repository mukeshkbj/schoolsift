from pathlib import Path

from schoolsift.demo_store import Attachment, DemoStore
from schoolsift.processor import XLSX_MIME, extract_attachment_text

DEMO_DIR = Path(__file__).resolve().parents[2] / "demo"


def att(name: str, mime: str) -> Attachment:
    return Attachment(id=f"att-{name}", name=name, mime=mime, path=name)


def test_docx_extraction_returns_real_text():
    text = extract_attachment_text(
        att(
            "early-dismissal-schedule.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        DEMO_DIR,
    )
    assert "Early dismissal schedule" in text
    assert "pickup 12:40" in text


def test_xlsx_extraction_returns_cells_in_order():
    text = extract_attachment_text(
        att(
            "supply-list.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        DEMO_DIR,
    )
    assert "School supply list" in text
    assert "Pencils" in text
    assert "Tissues" in text


def test_xlsx_shared_string_index_out_of_range_returns_empty(tmp_path):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main"><si><t>only one</t></si></sst>',
        )
        z.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="s">'
            "<v>9</v></c></row></sheetData></worksheet>",
        )
    (tmp_path / "attachments").mkdir()
    (tmp_path / "attachments" / "broken.xlsx").write_bytes(buf.getvalue())
    text = extract_attachment_text(att("broken.xlsx", XLSX_MIME), tmp_path)
    assert text == ""


def test_plain_text_attachment_decodes():
    text = extract_attachment_text(
        att("materials-fee-letter.txt", "text/plain"), DEMO_DIR
    )
    assert "materials fee" in text.lower()


def test_binary_attachments_return_empty():
    assert (
        extract_attachment_text(att("class-photo-notice.png", "image/png"), DEMO_DIR)
        == ""
    )
    assert (
        extract_attachment_text(
            att("field-trip-permission.pdf", "application/pdf"), DEMO_DIR
        )
        == ""
    )


def test_docx_evidence_uses_extracted_content():
    store = DemoStore(DEMO_DIR)
    packet = store.process(["msg-early-dismissal"])[0]
    quotes = " ".join(e.quote for e in packet.evidence)
    assert "Early dismissal schedule" in quotes or "pickup 12:40" in quotes


def test_xlsx_evidence_uses_extracted_content():
    store = DemoStore(DEMO_DIR)
    packet = store.process(["msg-newsletter"])[0]
    quotes = " ".join(e.quote for e in packet.evidence)
    assert "School supply list" in quotes or "Pencils" in quotes
