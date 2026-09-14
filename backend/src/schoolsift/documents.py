from __future__ import annotations

import zipfile
from html.parser import HTMLParser
from io import BytesIO

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pypdf import PdfReader

from .errors import UnsupportedDocumentError
from .models import DocumentContent, DocumentRecord

MAX_DOCUMENT_BYTES = int(4.5 * 1024 * 1024)
MAX_IMAGE_BYTES = int(3.75 * 1024 * 1024)
MAX_DOCUMENTS = 5
MAX_IMAGES = 20
MAX_PDF_PAGES = 100
MAX_XML_BYTES = 8 * 1024 * 1024
MAX_SHEETS = 50
MAX_TEXT_CHARS = 2 * 1024 * 1024

_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        if tag in ("br", "p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html)
    return " ".join("".join(parser.parts).split())


def _unreadable(record: DocumentRecord, detail: str) -> UnsupportedDocumentError:
    return UnsupportedDocumentError(
        f"Attachment '{record.name}' {detail}; review it in your inbox."
    )


def _member(zf: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = zf.getinfo(name)
    except KeyError:
        return b""
    if info.file_size > MAX_XML_BYTES:
        raise UnsupportedDocumentError(
            "Document contains an oversized internal part; review it in your inbox."
        )
    return zf.read(name)


def _docx_text(content: bytes, record: DocumentRecord) -> str:
    try:
        with zipfile.ZipFile(BytesIO(content)) as zf:
            data = _member(zf, "word/document.xml")
    except zipfile.BadZipFile as e:
        raise _unreadable(record, "is not a readable document") from e
    if not data:
        raise _unreadable(record, "has no readable text")
    try:
        root = ElementTree.fromstring(data)
    except (ElementTree.ParseError, DefusedXmlException) as e:
        raise _unreadable(record, "is not a readable document") from e
    paras: list[str] = []
    total = 0
    for para in root.iter(_W + "p"):
        text = "".join(t.text or "" for t in para.iter(_W + "t"))
        if text.strip():
            paras.append(text)
            total += len(text)
            if total > MAX_TEXT_CHARS:
                raise _unreadable(record, "is too large to read")
    return "\n".join(paras)


def _xlsx_text(content: bytes, record: DocumentRecord) -> str:
    try:
        with zipfile.ZipFile(BytesIO(content)) as zf:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in zf.namelist():
                try:
                    sroot = ElementTree.fromstring(_member(zf, "xl/sharedStrings.xml"))
                except (ElementTree.ParseError, DefusedXmlException) as e:
                    raise _unreadable(record, "is not a readable document") from e
                for si in sroot.iter(_S + "si"):
                    shared.append("".join(t.text or "" for t in si.iter(_S + "t")))
            sheets = sorted(
                n
                for n in zf.namelist()
                if n.startswith("xl/worksheets/") and n.endswith(".xml")
            )
            if len(sheets) > MAX_SHEETS:
                raise _unreadable(record, "has too many sheets")
            lines: list[str] = []
            total = 0
            for sheet in sheets:
                try:
                    root = ElementTree.fromstring(_member(zf, sheet))
                except (ElementTree.ParseError, DefusedXmlException) as e:
                    raise _unreadable(record, "is not a readable document") from e
                for row in root.iter(_S + "row"):
                    cells: list[str] = []
                    for cell in row.iter(_S + "c"):
                        if cell.get("t") == "s":
                            v = cell.find(_S + "v")
                            if v is not None and v.text is not None:
                                idx = int(v.text)
                                cells.append(
                                    shared[idx] if 0 <= idx < len(shared) else ""
                                )
                        elif cell.get("t") == "inlineStr":
                            cells.append(
                                "".join(t.text or "" for t in cell.iter(_S + "t"))
                            )
                        else:
                            v = cell.find(_S + "v")
                            cells.append(v.text or "" if v is not None else "")
                    line = " | ".join(x for x in cells if x)
                    if line:
                        lines.append(line)
                        total += len(line)
                        if total > MAX_TEXT_CHARS:
                            raise _unreadable(record, "is too large to read")
    except zipfile.BadZipFile as e:
        raise _unreadable(record, "is not a readable document") from e
    return "\n".join(lines)


def _pdf(content: bytes, record: DocumentRecord) -> tuple[str, list[str]]:
    try:
        reader = PdfReader(BytesIO(content))
    except Exception as e:
        raise _unreadable(record, "is not a readable document") from e
    if reader.is_encrypted:
        raise _unreadable(record, "is password-protected")
    if len(reader.pages) > MAX_PDF_PAGES:
        raise _unreadable(record, f"exceeds the {MAX_PDF_PAGES}-page limit")
    try:
        text = "\n".join(p.extract_text() or "" for p in reader.pages)
    except Exception as e:
        raise _unreadable(record, "is not a readable document") from e
    try:
        fields = list((reader.get_fields() or {}).keys())
    except Exception:
        fields = []
    return text, fields


def _text(content: bytes, record: DocumentRecord) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as e:
        raise _unreadable(record, "is not readable text") from e


_GENERIC_MIMES = {
    "",
    "application/octet-stream",
    "binary/octet-stream",
    "application/x-pdf",
}
_EXTENSION_MIMES = {
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".docx": _DOCX,
    ".xlsx": _XLSX,
}


def effective_mime(name: str, mime: str, content: bytes) -> str:
    """Providers often label attachments application/octet-stream; sniff those."""
    declared = mime.split(";")[0].strip().lower()
    if declared not in _GENERIC_MIMES:
        return declared
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    if content.startswith(b"\x89PNG"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    sniffed = _EXTENSION_MIMES.get(f".{ext}", declared)
    if sniffed in (_DOCX, _XLSX) and not content.startswith(b"PK\x03\x04"):
        return declared
    return sniffed


def read_document(record: DocumentRecord, content: bytes) -> DocumentContent:
    mime = effective_mime(record.name, record.mime, content)
    if len(content) > MAX_DOCUMENT_BYTES:
        raise _unreadable(record, "exceeds the 4.5 MB document limit")
    text = ""
    fields: list[str] = []
    if mime in ("text/plain", "text/csv", "text/markdown", "text/x-markdown"):
        text = _text(content, record)
    elif mime == "text/html":
        text = html_to_text(_text(content, record))
    elif mime == _DOCX:
        text = _docx_text(content, record)
    elif mime == _XLSX:
        text = _xlsx_text(content, record)
    elif mime == "application/pdf":
        text, fields = _pdf(content, record)
    elif mime in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        pass
    else:
        raise _unreadable(record, f"has an unsupported type ({mime})")
    return DocumentContent(
        document_id=record.id,
        name=record.name,
        mime=mime,
        text=text,
        acroform_fields=fields,
        source_bytes=content,
    )


def fill_pdf_form(
    source: bytes, fields: dict[str, str | list[str] | tuple[str, str, float]]
) -> bytes:
    from pypdf import PdfWriter

    try:
        reader = PdfReader(BytesIO(source))
    except Exception as e:
        raise UnsupportedDocumentError("The attachment is not a readable PDF.") from e
    if reader.is_encrypted:
        raise UnsupportedDocumentError("The PDF is password-protected.")
    try:
        known = set((reader.get_fields() or {}).keys())
    except Exception as e:
        raise UnsupportedDocumentError("The PDF form fields could not be read.") from e
    if not known:
        raise UnsupportedDocumentError("The PDF has no fillable form fields.")
    missing = [name for name in fields if name not in known]
    if missing:
        raise UnsupportedDocumentError(
            f"The PDF form has no field named '{missing[0]}'."
        )
    writer = PdfWriter()
    writer.append(reader)
    for page in writer.pages:
        writer.update_page_form_field_values(page, fields, auto_regenerate=False)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()
