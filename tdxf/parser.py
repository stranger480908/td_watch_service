"""
Streaming parser for USPTO Trademark Daily XML File (TDXF) - Applications.

Product short name: TRTDXFAP
Schema: TRADEMARK-APPLICATIONS-DAILY DTD v2.0 (2004-11-08)

Design notes
------------
A daily file holds roughly 50k-100k <case-file> elements. We use lxml.iterparse
and clear each element after processing so memory stays flat regardless of file
size. Never load these with ElementTree.parse().

Document shape (from the DTD):

    trademark-applications-daily
      version (version-no, version-date)
      application-information
        file-segments
          file-segment            -- always "TRMK" for applications
          action-keys
            action-key            -- applies to the case-files that follow it
            case-file*

The action-key is what USPTO did to the record in this run. It is emitted
*before* the case-files it governs, so we latch the most recent one.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from lxml import etree

from . import statements as stmt


def _text(el: etree._Element | None) -> str | None:
    """Element text, normalised. Empty elements in this DTD mean 'no value'."""
    if el is None:
        return None
    if el.text is None:
        return None
    t = el.text.strip()
    return t or None


def _child_text(parent: etree._Element | None, tag: str) -> str | None:
    if parent is None:
        return None
    return _text(parent.find(tag))


def _parse_date(raw: str | None) -> date | None:
    """USPTO dates are YYYYMMDD. Absent/unknown is encoded as 0 or 00000000."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw or set(raw) == {"0"}:
        return None
    if len(raw) != 8 or not raw.isdigit():
        return None
    y, m, d = int(raw[0:4]), int(raw[4:6]), int(raw[6:8])
    if not (1800 <= y <= 2200 and 1 <= m <= 12 and 1 <= d <= 31):
        return None
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _flag(raw: str | None) -> bool:
    """The *-in elements are 'T'/'F' boolean flags."""
    return (raw or "").strip().upper() == "T"


@dataclass
class Classification:
    primary_code: str | None = None
    international_codes: list[str] = field(default_factory=list)
    us_codes: list[str] = field(default_factory=list)
    status_code: str | None = None
    first_use_anywhere_date: date | None = None
    first_use_in_commerce_date: date | None = None


@dataclass
class Statement:
    type_code: str | None = None
    text: str | None = None

    @property
    def kind(self) -> str | None:
        return stmt.kind(self.type_code)

    @property
    def nice_class(self) -> str | None:
        return stmt.gs_class(self.type_code)

    @property
    def is_live_goods(self) -> bool:
        return stmt.is_live_goods(self.type_code)

    @property
    def clean_text(self) -> str:
        """Goods text with removed-goods markup stripped."""
        return stmt.clean_goods_text(self.text, self.type_code)


@dataclass
class ProsecutionEvent:
    """One case-file-event-statement. Carries the opposition-extension codes."""

    code: str | None = None
    type: str | None = None
    description: str | None = None
    date: date | None = None
    number: str | None = None


@dataclass
class Owner:
    party_name: str | None = None
    party_type: str | None = None
    legal_entity_type_code: str | None = None
    entity_statement: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    postcode: str | None = None
    nationality: str | None = None
    dba_aka_text: str | None = None


@dataclass
class CaseFile:
    """One trademark record as extracted from a daily file."""

    serial_number: str = ""
    registration_number: str | None = None
    transaction_date: date | None = None
    action_key: str | None = None

    # case-file-header
    mark_identification: str | None = None
    mark_drawing_code: str | None = None
    filing_date: date | None = None
    registration_date: date | None = None
    status_code: str | None = None
    status_date: date | None = None
    published_for_opposition_date: date | None = None
    abandonment_date: date | None = None
    cancellation_date: date | None = None
    renewal_date: date | None = None
    attorney_name: str | None = None
    attorney_docket_number: str | None = None
    law_office_assigned_location_code: str | None = None
    standard_characters_claimed: bool = False
    opposition_pending: bool = False
    cancellation_pending: bool = False
    section_2f: bool = False
    intent_to_use: bool = False

    classifications: list[Classification] = field(default_factory=list)
    statements: list[Statement] = field(default_factory=list)
    events: list[ProsecutionEvent] = field(default_factory=list)
    owners: list[Owner] = field(default_factory=list)
    correspondent: list[str] = field(default_factory=list)

    # ---- derived views the scoring engine consumes -------------------------

    @property
    def nice_classes(self) -> list[str]:
        """Sorted unique international (Nice) classes across all classifications."""
        out: set[str] = set()
        for c in self.classifications:
            out.update(c.international_codes)
            if c.primary_code:
                out.add(c.primary_code)
        return sorted(x for x in out if x)

    @property
    def goods_services(self) -> list[tuple[str | None, str]]:
        """
        (nice_class, description) pairs for goods the mark CURRENTLY covers.

        Statements consisting only of removed goods are dropped, and embedded
        removed-goods markup is stripped. Use .raw_goods_services if you need
        what USPTO actually emitted.
        """
        out = []
        for s in self.statements:
            if s.kind != stmt.GOODS_SERVICES:
                continue
            cleaned = s.clean_text
            if cleaned:
                out.append((s.nice_class, cleaned))
        return out

    @property
    def raw_goods_services(self) -> list[tuple[str | None, str]]:
        return [
            (s.nice_class, s.text)
            for s in self.statements
            if s.kind == stmt.GOODS_SERVICES and s.text
        ]

    @property
    def goods_services_text(self) -> str:
        return " ".join(t for _, t in self.goods_services)

    def _texts(self, k: str) -> list[str]:
        return [s.text for s in self.statements if s.kind == k and s.text]

    @property
    def pseudo_marks(self) -> list[str]:
        """USPTO's own alternate-spelling field. Free phonetic signal."""
        return self._texts(stmt.PSEUDO_MARK)

    @property
    def translations(self) -> list[str]:
        return self._texts(stmt.TRANSLATION)

    @property
    def transliterations(self) -> list[str]:
        """TLIT is a FOUR-character prefix; slicing [:2] silently loses these."""
        return self._texts(stmt.TRANSLITERATION)

    @property
    def mark_overflow(self) -> list[str]:
        return self._texts(stmt.MARK_OVERFLOW)

    @property
    def full_mark_text(self) -> str:
        """mark-identification plus any MK overflow. Long marks get truncated."""
        parts = [self.mark_identification or ""] + self.mark_overflow
        return " ".join(p for p in parts if p).strip()

    @property
    def opposition_extension_events(self) -> list[ProsecutionEvent]:
        """
        Codes 1000-1060 cover requests to extend time to oppose and their
        grants. These identify parties with proven need and proven budget.
        """
        out = []
        for e in self.events:
            if e.code and e.code.isdigit() and 1000 <= int(e.code) <= 1060:
                out.append(e)
        return out

    @property
    def owner_names(self) -> list[str]:
        return [o.party_name for o in self.owners if o.party_name]

    def content_hash(self) -> str:
        """
        Stable hash over everything we persist, used for change detection.

        transaction_date and action_key are excluded on purpose: they change on
        every run that touches the record and would make every record look
        modified.
        """
        payload = asdict(self)
        payload.pop("transaction_date", None)
        payload.pop("action_key", None)
        blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _build_case_file(el: etree._Element, action_key: str | None) -> CaseFile:
    cf = CaseFile(
        serial_number=_child_text(el, "serial-number") or "",
        registration_number=_child_text(el, "registration-number"),
        transaction_date=_parse_date(_child_text(el, "transaction-date")),
        action_key=action_key,
    )

    h = el.find("case-file-header")
    if h is not None:
        cf.mark_identification = _child_text(h, "mark-identification")
        cf.mark_drawing_code = _child_text(h, "mark-drawing-code")
        cf.filing_date = _parse_date(_child_text(h, "filing-date"))
        cf.registration_date = _parse_date(_child_text(h, "registration-date"))
        cf.status_code = _child_text(h, "status-code")
        cf.status_date = _parse_date(_child_text(h, "status-date"))
        cf.published_for_opposition_date = _parse_date(
            _child_text(h, "published-for-opposition-date")
        )
        cf.abandonment_date = _parse_date(_child_text(h, "abandonment-date"))
        cf.cancellation_date = _parse_date(_child_text(h, "cancellation-date"))
        cf.renewal_date = _parse_date(_child_text(h, "renewal-date"))
        cf.attorney_name = _child_text(h, "attorney-name")
        cf.attorney_docket_number = _child_text(h, "attorney-docket-number")
        cf.law_office_assigned_location_code = _child_text(
            h, "law-office-assigned-location-code"
        )
        cf.standard_characters_claimed = _flag(
            _child_text(h, "standard-characters-claimed-in")
        )
        cf.opposition_pending = _flag(_child_text(h, "opposition-pending-in"))
        cf.cancellation_pending = _flag(_child_text(h, "cancellation-pending-in"))
        cf.section_2f = _flag(_child_text(h, "section-2f-in"))
        cf.intent_to_use = _flag(_child_text(h, "intent-to-use-in"))

    for c in el.findall("classifications/classification"):
        cls = Classification(
            primary_code=_child_text(c, "primary-code"),
            international_codes=[
                t for t in (_text(x) for x in c.findall("international-code")) if t
            ],
            us_codes=[t for t in (_text(x) for x in c.findall("us-code")) if t],
            status_code=_child_text(c, "status-code"),
            first_use_anywhere_date=_parse_date(_child_text(c, "first-use-anywhere-date")),
            first_use_in_commerce_date=_parse_date(
                _child_text(c, "first-use-in-commerce-date")
            ),
        )
        cf.classifications.append(cls)

    for s in el.findall("case-file-statements/case-file-statement"):
        # <text> can repeat; join rather than dropping the tail.
        texts = [t for t in (_text(x) for x in s.findall("text")) if t]
        cf.statements.append(
            Statement(
                type_code=_child_text(s, "type-code"),
                text=stmt.join_text_chunks(texts) or None,
            )
        )

    for e in el.findall("case-file-event-statements/case-file-event-statement"):
        cf.events.append(
            ProsecutionEvent(
                code=_child_text(e, "code"),
                type=_child_text(e, "type"),
                description=_child_text(e, "description-text"),
                date=_parse_date(_child_text(e, "date")),
                number=_child_text(e, "number"),
            )
        )

    for o in el.findall("case-file-owners/case-file-owner"):
        nat = o.find("nationality")
        nationality = None
        if nat is not None:
            for tag in ("country", "state", "other"):
                nationality = _child_text(nat, tag)
                if nationality:
                    break
        cf.owners.append(
            Owner(
                party_name=_child_text(o, "party-name"),
                party_type=_child_text(o, "party-type"),
                legal_entity_type_code=_child_text(o, "legal-entity-type-code"),
                entity_statement=_child_text(o, "entity-statement"),
                city=_child_text(o, "city"),
                state=_child_text(o, "state"),
                country=_child_text(o, "country"),
                postcode=_child_text(o, "postcode"),
                nationality=nationality,
                dba_aka_text=_child_text(o, "dba-aka-text"),
            )
        )

    corr = el.find("correspondent")
    if corr is not None:
        cf.correspondent = [
            t
            for t in (_child_text(corr, f"address-{i}") for i in range(1, 6))
            if t
        ]

    return cf


def iter_case_files(source) -> Iterator[CaseFile]:
    """
    Stream CaseFile records from an open XML file object or path.

    Memory stays flat: each <case-file> is cleared once consumed, and preceding
    siblings are dropped so the partially-built tree never accumulates.
    """
    action_key: str | None = None

    context = etree.iterparse(
        source,
        events=("end",),
        tag=("action-key", "case-file"),
        load_dtd=False,
        resolve_entities=False,
        recover=True,
    )

    for _, el in context:
        if el.tag == "action-key":
            action_key = _text(el)
        else:
            yield _build_case_file(el, action_key)

        el.clear()
        parent = el.getparent()
        if parent is not None:
            while el.getprevious() is not None:
                del parent[0]

    del context


def iter_case_files_from_zip(zip_path: str | Path) -> Iterator[CaseFile]:
    """
    Stream records straight out of the daily .zip without extracting to disk.

    The daily archives contain a single XML member (plus occasional docs), so we
    take every .xml member in order.
    """
    with zipfile.ZipFile(zip_path) as zf:
        members = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        if not members:
            raise ValueError(f"no .xml member found in {zip_path}")
        for name in members:
            with zf.open(name) as fh:
                # zipfile streams are not seekable in all Python builds; wrap.
                yield from iter_case_files(io.BufferedReader(fh))  # type: ignore[arg-type]
