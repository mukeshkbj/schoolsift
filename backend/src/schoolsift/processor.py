from __future__ import annotations

import re
import zipfile
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from xml.etree import ElementTree as ET

from .domain import (
    ActionPacketDraft,
    CalendarProposal,
    EscalationProposal,
    EvidenceSpan,
    PdfFormProposal,
    ProposalPayload,
    ReplyProposal,
)

if TYPE_CHECKING:
    from .demo_store import Attachment, DemoContext, DemoMessage

DEMO_YEAR = 2026
MAX_ATTACHMENT_TEXT = 100_000

INJECTION_RE = re.compile(
    r"ignore\s+(all\s+|any\s+)?(previous|prior)\s+instructions?"
    r"|system\s+note"
    r"|you\s+are\s+authorized\s+to",
    re.IGNORECASE,
)
PAYMENT_RE = re.compile(r"\bfee\b|\bpayment\b|\$\s?\d", re.IGNORECASE)
SIGNATURE_RE = re.compile(r"\bsign(ed|ature|ing)?\b", re.IGNORECASE)
REPLY_REQUEST_RE = re.compile(
    r"\breply\b|\bconfirm(ing|ation)?\b|permission|volunteers?\s+needed",
    re.IGNORECASE,
)
DEADLINE_RE = re.compile(
    r"\b(?:by|due)\b[^.\n]*?\b("
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|"
    r"Oct|Nov|Dec)\.?\s+\d{1,2}(?:,\s*\d{4})?)",
    re.IGNORECASE,
)
DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|"
    r"Oct|Nov|Dec)\.?\s+(\d{1,2})(?:,\s*(\d{4}))?",
    re.IGNORECASE,
)
TIME_RANGE_RE = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*[-–]\s*(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\b",  # noqa: RUF001
    re.IGNORECASE,
)
TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\b", re.IGNORECASE)

MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))  # noqa: S314
    return " ".join(t.text or "" for t in root.iter(f"{W_NS}t"))


def _xlsx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            sroot = ET.fromstring(z.read("xl/sharedStrings.xml"))  # noqa: S314
            for si in sroot.iter(f"{S_NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{S_NS}t")))
        lines: list[str] = []
        for name in sorted(
            n for n in names if n.startswith("xl/worksheets/") and n.endswith(".xml")
        ):
            root = ET.fromstring(z.read(name))  # noqa: S314
            for row in root.iter(f"{S_NS}row"):
                values: list[str] = []
                for cell in row.iter(f"{S_NS}c"):
                    kind = cell.get("t")
                    v = cell.find(f"{S_NS}v")
                    if kind == "s" and v is not None and v.text is not None:
                        values.append(shared[int(v.text)])
                    elif kind == "inlineStr":
                        inline = cell.find(f"{S_NS}is")
                        if inline is not None:
                            values.append(
                                "".join(t.text or "" for t in inline.iter(f"{S_NS}t"))
                            )
                    elif v is not None and v.text is not None:
                        values.append(v.text)
                if values:
                    lines.append(" ".join(values))
    return "\n".join(lines)


def extract_attachment_text(attachment: Attachment, demo_dir: Path) -> str:
    path = demo_dir / "attachments" / attachment.path
    try:
        if attachment.mime.startswith("text/"):
            return path.read_bytes()[:MAX_ATTACHMENT_TEXT].decode(
                "utf-8", errors="replace"
            )
        if attachment.mime == DOCX_MIME:
            return _docx_text(path)[:MAX_ATTACHMENT_TEXT]
        if attachment.mime == XLSX_MIME:
            return _xlsx_text(path)[:MAX_ATTACHMENT_TEXT]
    except (OSError, KeyError, IndexError, zipfile.BadZipFile, ET.ParseError):
        return ""
    return ""


def _to_24h(hour: int, minute: int, ampm: str) -> time:
    hour = hour % 12
    if ampm.upper() == "PM":
        hour += 12
    return time(hour, minute)


def _find_times(text: str) -> list[time]:
    found: list[tuple[int, time]] = []
    for m in TIME_RANGE_RE.finditer(text):
        suffix = m.group(5)
        found.append(
            (m.start(), _to_24h(int(m.group(1)), int(m.group(2) or 0), suffix))
        )
        found.append(
            (
                m.start() + 1,
                _to_24h(int(m.group(3)), int(m.group(4) or 0), suffix),
            )
        )
    for m in TIME_RE.finditer(text):
        found.append(
            (m.start(), _to_24h(int(m.group(1)), int(m.group(2) or 0), m.group(3)))
        )
    return [t for _, t in sorted(found, key=lambda p: p[0])]


def _find_dates(text: str) -> list[datetime]:
    return [
        datetime(
            int(m.group(3) or DEMO_YEAR),
            MONTHS[m.group(1).lower().rstrip(".")],
            int(m.group(2)),
            tzinfo=UTC,
        )
        for m in DATE_RE.finditer(text)
    ]


def _line_containing(text: str, needle: str) -> str:
    for line in text.splitlines():
        if needle.lower() in line.lower():
            return line.strip()
    return needle


def _event_window(body: str) -> tuple[datetime, datetime] | None:
    dates = _find_dates(body)
    times = _find_times(body)
    if not dates or not times:
        return None
    start = datetime.combine(dates[0].date(), times[0], tzinfo=UTC)
    later = [
        t for t in times if datetime.combine(dates[0].date(), t, tzinfo=UTC) > start
    ]
    end = (
        datetime.combine(dates[0].date(), later[-1], tzinfo=UTC)
        if later
        else start + timedelta(minutes=30)
    )
    return start, end


def _find_deadline(
    body: str, event: tuple[datetime, datetime] | None
) -> datetime | None:
    match = DEADLINE_RE.search(body)
    if match:
        return _find_dates(match.group(1))[0]
    if event is not None:
        return event[0]
    return None


def _urgency(
    now: datetime, deadline: datetime | None, escalated: bool
) -> Literal["none", "soon", "urgent"]:
    if deadline is not None:
        days = (deadline - now).days
        if days <= 3:
            return "urgent"
        if days <= 14:
            return "soon"
    if escalated:
        return "soon"
    return "none"


def _first_informative_sentence(body: str) -> str:
    text = re.sub(r"^\s*(dear|hello|hi)\b[^\n]*\n+", "", body.strip(), flags=re.I)
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if _find_dates(sentence) or len(sentence) > 60:
            return sentence.strip()
    return text.splitlines()[0] if text else ""


def _acroform_fields(pdf_path: Path) -> list[str]:
    from pypdf import PdfReader

    try:
        fields = PdfReader(str(pdf_path)).get_fields()
    except Exception:
        return []
    return list(fields.keys()) if fields else []


def process_message(
    message: DemoMessage, context: DemoContext, demo_dir: Path
) -> ActionPacketDraft:
    body = message.body
    attachment_texts = {
        a.id: extract_attachment_text(a, demo_dir) for a in message.attachments
    }
    full_text = body + "\n" + "\n".join(attachment_texts.values())
    evidence: list[EvidenceSpan] = []
    uncertainties: list[str] = []
    proposals: list[ProposalPayload] = []

    if not message.source.confirmed:
        uncertainties.append(
            f"'{message.sender.email}' is not a confirmed school source. "
            "Its content was not treated as trusted school correspondence."
        )
        proposals.append(
            EscalationProposal(
                kind="escalation",
                reason="uncertain",
                detail="Possible school-related sender. Confirm this source before "
                "SchoolSift prepares any action from it.",
            )
        )
        evidence.append(
            EvidenceSpan(source="header", quote=f"From: {message.sender.email}")
        )
        return ActionPacketDraft(
            source_message_id=message.id,
            school_source_id=None,
            summary=_first_informative_sentence(body),
            child=None,
            deadline=None,
            urgency="none",
            information_only=False,
            evidence=evidence,
            uncertainties=uncertainties,
            proposals=proposals,
        )

    child = next(
        (c.name for c in context.children if c.name.lower() in body.lower()), None
    )
    if child:
        evidence.append(
            EvidenceSpan(source="body", quote=_line_containing(body, child))
        )

    event = _event_window(body)
    deadline = _find_deadline(body, event)
    if deadline:
        evidence.append(
            EvidenceSpan(
                source="body",
                quote=_line_containing(body, deadline.strftime("%B %-d")),
            )
        )

    injected = bool(INJECTION_RE.search(full_text))
    wants_payment = bool(PAYMENT_RE.search(full_text))
    wants_signature = bool(SIGNATURE_RE.search(full_text))

    if injected:
        evidence.append(
            EvidenceSpan(source="body", quote=_line_containing(full_text, "ignore"))
        )
        uncertainties.append(
            "This message contains text addressed to an automated system "
            "('ignore ... instructions'). It was treated as untrusted content, "
            "never as a command."
        )

    if injected or wants_payment or wants_signature:
        if wants_payment:
            proposals.append(
                EscalationProposal(
                    kind="escalation",
                    reason="payment",
                    detail="The message requests a payment or fee. Payments are "
                    "never executed by SchoolSift; handle it with the school directly.",
                )
            )
            evidence.append(
                EvidenceSpan(source="body", quote=_line_containing(full_text, "fee"))
            )
        if wants_signature:
            proposals.append(
                EscalationProposal(
                    kind="escalation",
                    reason="signature",
                    detail="The message requests a signature. Signatures are never "
                    "executed by SchoolSift; review and sign it yourself.",
                )
            )
            evidence.append(
                EvidenceSpan(source="body", quote=_line_containing(full_text, "sign"))
            )
        if not proposals:
            proposals.append(
                EscalationProposal(
                    kind="escalation",
                    reason="uncertain",
                    detail="Suspicious instruction-like text; queued for "
                    "manual review.",
                )
            )
        return ActionPacketDraft(
            source_message_id=message.id,
            school_source_id=message.source.id,
            summary=_first_informative_sentence(body),
            child=child,
            deadline=deadline,
            urgency=_urgency(message.received_at, deadline, escalated=True),
            information_only=False,
            evidence=evidence,
            uncertainties=uncertainties,
            proposals=proposals,
        )

    for att in message.attachments:
        if att.mime == "application/pdf":
            fields = _acroform_fields(demo_dir / "attachments" / att.path)
            if fields:
                values = {
                    name: (
                        child or ""
                        if "student" in name or "child" in name
                        else context.caregivers[0].name
                        if "guardian" in name or "parent" in name
                        else ""
                    )
                    for name in fields
                }
                proposals.append(
                    PdfFormProposal(
                        kind="pdf_form",
                        document_name=att.name,
                        fields=values,
                    )
                )
                evidence.append(
                    EvidenceSpan(
                        source=att.name,
                        quote=f"Fillable form fields: {', '.join(fields)}",
                    )
                )
                blanks = [k for k, v in values.items() if not v]
                if blanks:
                    uncertainties.append(
                        f"Form fields left for caregiver review: {', '.join(blanks)}."
                    )
        elif att.mime in {"image/png", "image/jpeg"}:
            evidence.append(
                EvidenceSpan(source=att.name, quote=f"Attached notice: {att.name}")
            )
        else:
            text = attachment_texts.get(att.id, "")
            if text:
                first = text.splitlines()[0][:160]
                evidence.append(EvidenceSpan(source=att.name, quote=first))
            else:
                evidence.append(
                    EvidenceSpan(
                        source=att.name, quote=f"Attached document: {att.name}"
                    )
                )

    if event is not None:
        conflict = next(
            (
                e
                for e in context.family_calendar
                if e.starts_at < event[1] and event[0] < e.ends_at
            ),
            None,
        )
        if conflict is not None:
            uncertainties.append(
                f"The proposed time overlaps existing family event "
                f"'{conflict.title}' ({conflict.starts_at.isoformat()} to "
                f"{conflict.ends_at.isoformat()}). A calendar entry was NOT "
                "prepared automatically."
            )
            proposals.append(
                EscalationProposal(
                    kind="escalation",
                    reason="uncertain",
                    detail=f"Event on {event[0].date()} conflicts with "
                    f"'{conflict.title}'. Review before adding to the calendar.",
                )
            )
        else:
            title = re.sub(r"\s*[-–(].*$", "", message.subject).strip()  # noqa: RUF001
            proposals.append(
                CalendarProposal(
                    kind="calendar",
                    title=f"{child} — {title}" if child else title,
                    starts_at=event[0],
                    ends_at=event[1],
                )
            )

    if REPLY_REQUEST_RE.search(body) and "no reply is needed" not in body.lower():
        proposals.insert(
            0,
            ReplyProposal(
                kind="reply",
                recipient=message.reply_to,
                subject=f"Re: {message.subject}",
                body=(
                    f"Dear {message.sender.name},\n\n"
                    f"Thank you for the update"
                    + (f" about {child}" if child else "")
                    + ". We have noted the details"
                    + (
                        f" and will respond by {deadline.strftime('%B %-d')}"
                        if deadline
                        else ""
                    )
                    + f".\n\n{context.caregivers[0].name}"
                ),
            ),
        )
        evidence.append(
            EvidenceSpan(source="body", quote=_line_containing(body, "reply"))
        )

    information_only = not proposals
    return ActionPacketDraft(
        source_message_id=message.id,
        school_source_id=message.source.id,
        summary=_first_informative_sentence(body),
        child=child,
        deadline=deadline,
        urgency=_urgency(message.received_at, deadline, escalated=False),
        information_only=information_only,
        evidence=evidence,
        uncertainties=uncertainties,
        proposals=proposals,
    )
