from __future__ import annotations

import zipfile
from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from schoolsift.documents import read_document
from schoolsift.errors import UnsupportedDocumentError
from schoolsift.models import DocumentRecord

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def record(name: str = "doc", mime: str = "text/plain") -> DocumentRecord:
    return DocumentRecord(
        id="doc-1",
        household_id="hh-1",
        message_id="msg-1",
        name=name,
        mime=mime,
        content_ref="ref-1",
        acroform_fields=[],
    )


def docx_bytes(text: str) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body>'
            f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
            "</w:body></w:document>",
        )
    return buf.getvalue()


def xlsx_bytes() -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main">'
            "<si><t>Item</t></si><si><t>Water bottle</t></si></sst>",
        )
        z.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main"><sheetData><row r="1">'
            '<c r="A1" t="s"><v>0</v></c>'
            '<c r="B1" t="s"><v>1</v></c></row></sheetData></worksheet>',
        )
    return buf.getvalue()


def pdf_bytes(*, with_field: bool = False, pages: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=200, height=200)
    if with_field:
        field = DictionaryObject(
            {
                NameObject("/FT"): NameObject("/Tx"),
                NameObject("/T"): TextStringObject("student_name"),
                NameObject("/V"): TextStringObject(""),
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Widget"),
                NameObject("/Rect"): ArrayObject(
                    [
                        NumberObject(0),
                        NumberObject(0),
                        NumberObject(100),
                        NumberObject(20),
                    ]
                ),
                NameObject("/P"): page.indirect_reference,
            }
        )
        ref = writer._add_object(field)
        page[NameObject("/Annots")] = ArrayObject([ref])
        writer._root_object[NameObject("/AcroForm")] = DictionaryObject(
            {NameObject("/Fields"): ArrayObject([ref])}
        )
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_text_roundtrip_and_source_bytes_excluded():
    doc = read_document(record(), b"Permission slip due Friday")
    assert doc.text == "Permission slip due Friday"
    assert doc.source_bytes == b"Permission slip due Friday"
    assert "source_bytes" not in doc.model_dump()
    assert "source_bytes" not in doc.model_dump_json()


def test_csv_and_markdown_are_text():
    assert read_document(record(mime="text/csv"), b"a,b\n1,2").text == "a,b\n1,2"
    assert read_document(record(mime="text/markdown"), b"# Hi").text == "# Hi"


def test_html_strips_scripts_and_tags():
    html = (
        b"<html><style>body{x:1}</style><p>Bring a <b>sack</b> lunch</p>"
        b"<script>alert(1)</script></html>"
    )
    doc = read_document(record(mime="text/html"), html)
    assert doc.text == "Bring a sack lunch"
    assert "alert" not in doc.text


def test_non_utf8_text_rejected():
    with pytest.raises(UnsupportedDocumentError):
        read_document(record(), b"\xff\xfe\x00bad")


def test_docx_text():
    doc = read_document(
        record(name="letter.docx", mime=DOCX_MIME),
        docx_bytes("Early dismissal at noon"),
    )
    assert doc.text == "Early dismissal at noon"


def test_xlsx_text():
    doc = read_document(record(name="supplies.xlsx", mime=XLSX_MIME), xlsx_bytes())
    assert "Item" in doc.text and "Water bottle" in doc.text


def test_bad_zip_rejected():
    with pytest.raises(UnsupportedDocumentError):
        read_document(record(mime=DOCX_MIME), b"not a zip")


def test_pdf_text_and_acroform_fields():
    doc = read_document(
        record(name="form.pdf", mime="application/pdf"),
        pdf_bytes(with_field=True),
    )
    assert doc.acroform_fields == ["student_name"]


def test_encrypted_pdf_rejected():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    buf = BytesIO()
    writer.write(buf)
    with pytest.raises(UnsupportedDocumentError):
        read_document(record(mime="application/pdf"), buf.getvalue())


def test_malformed_pdf_rejected():
    with pytest.raises(UnsupportedDocumentError):
        read_document(record(mime="application/pdf"), b"%PDF-broken")


def test_oversized_document_rejected():
    big = b"x" * (int(4.5 * 1024 * 1024) + 1)
    with pytest.raises(UnsupportedDocumentError) as e:
        read_document(record(name="big.txt"), big)
    assert "4.5 MB" in e.value.message


def test_too_many_pdf_pages_rejected():
    with pytest.raises(UnsupportedDocumentError):
        read_document(record(mime="application/pdf"), pdf_bytes(pages=101))


def test_image_returns_metadata_empty_text():
    doc = read_document(record(name="photo.png", mime="image/png"), b"\x89PNGimg")
    assert doc.text == ""
    assert doc.source_bytes == b"\x89PNGimg"


def test_octet_stream_pdf_sniffed_by_content():
    doc = read_document(
        record(name="Flu letter.pdf", mime="application/octet-stream"),
        pdf_bytes(with_field=True),
    )
    assert doc.mime == "application/pdf"
    assert doc.acroform_fields == ["student_name"]


def test_octet_stream_text_sniffed_by_extension():
    doc = read_document(
        record(name="notes.txt", mime="application/octet-stream"), b"hi"
    )
    assert doc.mime == "text/plain"
    assert doc.text == "hi"


def test_octet_stream_unknown_still_rejected():
    with pytest.raises(UnsupportedDocumentError):
        read_document(
            record(name="pack.bin", mime="application/octet-stream"), b"\x00\x01"
        )


def test_unsupported_mime_rejected():
    with pytest.raises(UnsupportedDocumentError):
        read_document(record(name="pack.zip", mime="application/zip"), b"PK")


def _form_pdf() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    field = DictionaryObject(
        {
            NameObject("/FT"): NameObject("/Tx"),
            NameObject("/T"): TextStringObject("student_name"),
            NameObject("/V"): TextStringObject(""),
            NameObject("/DA"): TextStringObject("/Helv 12 Tf 0 g"),
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/Rect"): ArrayObject(
                [
                    NumberObject(0),
                    NumberObject(0),
                    NumberObject(100),
                    NumberObject(20),
                ]
            ),
            NameObject("/P"): page.indirect_reference,
        }
    )
    field_ref = writer._add_object(field)
    page[NameObject("/Annots")] = ArrayObject([field_ref])
    font_ref = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
    )
    font_dict_ref = writer._add_object(
        DictionaryObject({NameObject("/Helv"): font_ref})
    )
    dr_ref = writer._add_object(DictionaryObject({NameObject("/Font"): font_dict_ref}))
    acro_ref = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Fields"): ArrayObject([field_ref]),
                NameObject("/DR"): dr_ref,
            }
        )
    )
    writer._root_object[NameObject("/AcroForm")] = acro_ref
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_fill_pdf_form_fills_fields_without_flattening():
    from pypdf import PdfReader

    from schoolsift.documents import fill_pdf_form

    filled = fill_pdf_form(_form_pdf(), {"student_name": "Kid A"})
    reader = PdfReader(BytesIO(filled))
    fields = reader.get_fields()
    assert fields is not None
    assert fields["student_name"]["/V"] == "Kid A"
    assert "/AcroForm" in reader.trailer["/Root"]


def test_fill_pdf_form_unknown_field_rejected():
    from schoolsift.documents import fill_pdf_form
    from schoolsift.errors import UnsupportedDocumentError

    with pytest.raises(UnsupportedDocumentError):
        fill_pdf_form(_form_pdf(), {"nope": "x"})


def test_fill_pdf_form_no_fields_rejected():
    from schoolsift.documents import fill_pdf_form
    from schoolsift.errors import UnsupportedDocumentError

    with pytest.raises(UnsupportedDocumentError):
        fill_pdf_form(pdf_bytes(), {"student_name": "x"})


def test_fill_pdf_form_non_pdf_rejected():
    from schoolsift.documents import fill_pdf_form
    from schoolsift.errors import UnsupportedDocumentError

    with pytest.raises(UnsupportedDocumentError):
        fill_pdf_form(b"not a pdf", {"f": "v"})
