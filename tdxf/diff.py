"""
Diff a parsed daily file against prior state and emit events.

The pipeline is deliberately event-shaped rather than snapshot-shaped: the
product sends alerts, and an alert is always *about something that happened*.
Storing only the current snapshot would force the alert layer to re-derive what
changed, which is where duplicate and missed alerts come from.

Publication is the event that matters most. Once a mark publishes in the
Official Gazette a 30-day opposition window opens, so PUBLISHED_FOR_OPPOSITION
carries a deadline and everything else is context.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import Protocol

from .parser import CaseFile

# Statutory opposition window from the Official Gazette publication date.
# Extendable on request; extensions arrive via the TTAB feed, not this one.
OPPOSITION_WINDOW_DAYS = 30


class EventType(StrEnum):
    NEW_APPLICATION = "NEW_APPLICATION"
    PUBLISHED_FOR_OPPOSITION = "PUBLISHED_FOR_OPPOSITION"
    PUBLICATION_DATE_CHANGED = "PUBLICATION_DATE_CHANGED"
    MARK_TEXT_CHANGED = "MARK_TEXT_CHANGED"
    CLASSES_CHANGED = "CLASSES_CHANGED"
    GOODS_SERVICES_CHANGED = "GOODS_SERVICES_CHANGED"
    OWNER_CHANGED = "OWNER_CHANGED"
    STATUS_CHANGED = "STATUS_CHANGED"
    REGISTERED = "REGISTERED"
    ABANDONED = "ABANDONED"
    CANCELLED = "CANCELLED"
    OPPOSITION_PENDING = "OPPOSITION_PENDING"


@dataclass(frozen=True)
class MarkState:
    """The subset of a record we persist for change detection."""

    serial_number: str
    content_hash: str
    mark_identification: str | None
    status_code: str | None
    published_for_opposition_date: date | None
    registration_date: date | None
    abandonment_date: date | None
    cancellation_date: date | None
    opposition_pending: bool
    nice_classes: tuple[str, ...]
    goods_services_text: str
    owner_names: tuple[str, ...]

    @classmethod
    def from_case_file(cls, cf: CaseFile) -> MarkState:
        return cls(
            serial_number=cf.serial_number,
            content_hash=cf.content_hash(),
            mark_identification=cf.mark_identification,
            status_code=cf.status_code,
            published_for_opposition_date=cf.published_for_opposition_date,
            registration_date=cf.registration_date,
            abandonment_date=cf.abandonment_date,
            cancellation_date=cf.cancellation_date,
            opposition_pending=cf.opposition_pending,
            nice_classes=tuple(cf.nice_classes),
            goods_services_text=cf.goods_services_text,
            owner_names=tuple(cf.owner_names),
        )


@dataclass
class Event:
    serial_number: str
    event_type: EventType
    observed_on: date
    old_value: str | None = None
    new_value: str | None = None
    opposition_deadline: date | None = None
    case_file: CaseFile | None = field(default=None, repr=False)

    @property
    def days_to_deadline(self) -> int | None:
        if self.opposition_deadline is None:
            return None
        return (self.opposition_deadline - self.observed_on).days


class PriorState(Protocol):
    def get(self, serial_number: str) -> MarkState | None: ...


class DictPriorState(dict):
    """In-memory prior state. Fine for tests and for the first backfill run."""

    def get(self, serial_number: str) -> MarkState | None:  # type: ignore[override]
        return dict.get(self, serial_number)

    def apply(self, state: MarkState) -> None:
        self[state.serial_number] = state


def opposition_deadline(publication_date: date | None) -> date | None:
    if publication_date is None:
        return None
    return publication_date + timedelta(days=OPPOSITION_WINDOW_DAYS)


def _s(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) or None
    return str(value)


def diff_record(
    cf: CaseFile,
    prior: MarkState | None,
    observed_on: date,
) -> list[Event]:
    """Compare one incoming record against its stored state."""
    now = MarkState.from_case_file(cf)
    events: list[Event] = []

    def emit(t: EventType, old=None, new=None, deadline: date | None = None) -> None:
        events.append(
            Event(
                serial_number=cf.serial_number,
                event_type=t,
                observed_on=observed_on,
                old_value=_s(old),
                new_value=_s(new),
                opposition_deadline=deadline,
                case_file=cf,
            )
        )

    if prior is None:
        emit(EventType.NEW_APPLICATION, None, cf.mark_identification)
        # A record can arrive already published (backfill, or a mark that
        # publishes before we first see it). Do not swallow the deadline.
        if now.published_for_opposition_date:
            emit(
                EventType.PUBLISHED_FOR_OPPOSITION,
                None,
                now.published_for_opposition_date,
                opposition_deadline(now.published_for_opposition_date),
            )
        return events

    if prior.content_hash == now.content_hash:
        return []

    if prior.published_for_opposition_date != now.published_for_opposition_date:
        if prior.published_for_opposition_date is None:
            emit(
                EventType.PUBLISHED_FOR_OPPOSITION,
                None,
                now.published_for_opposition_date,
                opposition_deadline(now.published_for_opposition_date),
            )
        elif now.published_for_opposition_date is not None:
            # Republication resets the clock. Treating this as a no-op is how
            # you miss a live deadline.
            emit(
                EventType.PUBLICATION_DATE_CHANGED,
                prior.published_for_opposition_date,
                now.published_for_opposition_date,
                opposition_deadline(now.published_for_opposition_date),
            )

    if prior.mark_identification != now.mark_identification:
        emit(EventType.MARK_TEXT_CHANGED, prior.mark_identification, now.mark_identification)

    if prior.nice_classes != now.nice_classes:
        emit(EventType.CLASSES_CHANGED, prior.nice_classes, now.nice_classes)

    if prior.goods_services_text != now.goods_services_text:
        emit(EventType.GOODS_SERVICES_CHANGED, None, None)

    if prior.owner_names != now.owner_names:
        emit(EventType.OWNER_CHANGED, prior.owner_names, now.owner_names)

    if prior.status_code != now.status_code:
        emit(EventType.STATUS_CHANGED, prior.status_code, now.status_code)

    if prior.registration_date is None and now.registration_date is not None:
        emit(EventType.REGISTERED, None, now.registration_date)

    if prior.abandonment_date is None and now.abandonment_date is not None:
        emit(EventType.ABANDONED, None, now.abandonment_date)

    if prior.cancellation_date is None and now.cancellation_date is not None:
        emit(EventType.CANCELLED, None, now.cancellation_date)

    if not prior.opposition_pending and now.opposition_pending:
        emit(EventType.OPPOSITION_PENDING, False, True)

    return events


def diff_stream(
    case_files: Iterable[CaseFile],
    prior: PriorState,
    observed_on: date,
) -> Iterator[Event]:
    """Diff a whole daily file. Yields events in file order."""
    for cf in case_files:
        if not cf.serial_number:
            continue
        yield from diff_record(cf, prior.get(cf.serial_number), observed_on)


def summarise(events: Iterable[Event]) -> Mapping[str, int]:
    counts: dict[str, int] = {}
    for e in events:
        counts[e.event_type.value] = counts.get(e.event_type.value, 0) + 1
    return dict(sorted(counts.items()))
