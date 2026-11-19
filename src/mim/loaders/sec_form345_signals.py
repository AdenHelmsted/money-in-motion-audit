#GOAL Detect insider sales and option exercises with traceable filing evidence.

"""Detect sale and exercise events, including corrections to prior filings."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import cache
from itertools import groupby

import psycopg

from .sec_form345_input import LoadError, parse_date
from .sec_form345_projection import Trade, cik, filing_timestamp, stable_id, trade_from_raw
from .sec_ingestion import existing_signals, remove_stale_signals, resolve_active_org, resolve_active_person, save_signal


DETECTOR = "sec_form345_v1"


@dataclass(frozen=True)
class Event:
    kind: str
    legs: tuple[Trade, ...]

    @property
    def primary(self):
        return self.legs[0]

    @property
    def signal_id(self):
        return stable_id("signal", self.kind, self.primary.record.external_id)

    @property
    def trade_key(self):
        return self.primary.transaction_on, self.primary.security


def _filing_events(trades):
    events = []
    exercises = defaultdict(list)
    for trade in trades:
        if trade.code == "S":
            events.append(Event("insider_sale", (trade,)))
        elif trade.code == "M":
            key = trade.transaction_on, trade.security, trade.reported_shares
            exercises[key].append(trade)

    for legs in exercises.values():
        shares_in = [leg for leg in legs if leg.record.table_name == "NONDERIV_TRANS" and leg.side == "A"]
        options_out = [leg for leg in legs if leg.record.table_name == "DERIV_TRANS" and leg.side == "D"]
        # Repeated identical match keys are paired in source-key order, consuming each leg once.
        by_source_key = lambda leg: leg.record.external_id
        pairs = list(zip(sorted(shares_in, key=by_source_key), sorted(options_out, key=by_source_key)))
        paired = {leg.raw_id for pair in pairs for leg in pair}
        events.extend(Event("insider_option_exercise", pair) for pair in pairs)
        events.extend(Event("insider_option_exercise", (leg,)) for leg in legs if leg.raw_id not in paired)
    return events


def _match_correction(event, prior_events):
    candidates = [prior for prior in prior_events if prior.kind == event.kind]
    if len(candidates) > 1:
        candidates = [prior for prior in candidates if prior.trade_key == event.trade_key]
    if len(candidates) > 1:
        candidates = [prior for prior in candidates if prior.primary.reported_shares == event.primary.reported_shares]
    return candidates[0] if len(candidates) == 1 else None


def _detect(records):
    originals = defaultdict(list)
    amendments = []
    emitted = {}

    for _, rows in groupby(records, key=lambda pair: pair[1].accession_number):
        filing = list(rows)
        # The last stored row carries the newest submission and owner evidence for this accession.
        header = filing[-1][1]
        submission = header.payload["SUBMISSION"]
        issuer = cik(submission["ISSUERCIK"])
        owners = {cik(owner["RPTOWNERCIK"]) for owner in header.payload["REPORTINGOWNER"]}
        form, _, amended = submission["DOCUMENT_TYPE"].strip().partition("/")
        events = []
        if len(owners) == 1:
            trades = []
            for raw_id, record in filing:
                record_owners = {cik(owner["RPTOWNERCIK"]) for owner in record.payload["REPORTINGOWNER"]}
                same_issuer = cik(record.payload["SUBMISSION"]["ISSUERCIK"]) == issuer
                if record_owners == owners and same_issuer and (trade := trade_from_raw(record, raw_id)) is not None:
                    trades.append(trade)
            events = _filing_events(trades)

        if amended:
            original_date = submission.get("DATE_OF_ORIG_SUB", "").strip()
            if original_date and events:
                amendments.append((header.observed_on, header.accession_number,
                                   (issuer, next(iter(owners)), form, parse_date(original_date)), events))
        else:
            for owner in owners:
                originals[issuer, owner, form, header.observed_on].append(events)
            emitted.update((event.signal_id, event) for event in events)

    for amendment_date, _, original_key, events in sorted(amendments, key=lambda filing: filing[:2]):
        prior_filings = originals[original_key]
        if len(prior_filings) != 1 or original_key[-1] > amendment_date:
            continue
        corrections = {}
        counts = Counter()
        for event in events:
            prior = _match_correction(event, prior_filings[0])
            if prior is not None:
                counts[prior.signal_id] += 1
                corrections[prior.signal_id] = event
        emitted.update((signal_id, event) for signal_id, event in corrections.items() if counts[signal_id] == 1)
    return emitted


def sync_signals(conn, records, provenance):
    desired = _detect(records)
    people = dict(conn.execute("SELECT value, person_id FROM core.person_identifiers WHERE scheme = 'sec_cik'").fetchall())
    issuers = dict(conn.execute(
        "SELECT value, organization_id FROM core.organization_identifiers WHERE scheme = 'sec_cik'"
    ).fetchall())
    active_person = cache(lambda identity: resolve_active_person(conn, identity))
    active_org = cache(lambda identity: resolve_active_org(conn, identity))
    existing = existing_signals(conn, DETECTOR)
    remove_stale_signals(conn, existing, desired.keys())

    for signal_id, event in desired.items():
        trade = event.primary
        record = trade.record
        submission = record.payload["SUBMISSION"]
        owner = record.payload["REPORTINGOWNER"][0]
        try:
            person_id = active_person(people[cik(owner["RPTOWNERCIK"])])
            issuer_id = active_org(issuers[cik(submission["ISSUERCIK"])])
            owner_name = owner["RPTOWNERNAME"].strip()
            issuer_name = submission["ISSUERNAME"].strip()
            action = "a sale" if event.kind == "insider_sale" else "an option exercise"
            evidence = sorted(leg.raw_id for leg in event.legs)
            fields = (person_id, issuer_id, event.kind, trade.transaction_on, trade.sale_amount_cents,
                      f"{owner_name} reported {action} involving {issuer_name} securities.", 1,
                      filing_timestamp(record.observed_on))
            save_signal(conn, DETECTOR, existing, signal_id, fields, evidence)
        except (KeyError, ValueError, psycopg.Error) as exc:
            reason = exc.diag.message_primary if isinstance(exc, psycopg.Error) else str(exc)
            uri, row_number = provenance.get(trade.raw_id, (None, None))
            raise LoadError(artifact_uri=uri, table_name=record.table_name,
                            accession_number=record.accession_number, row_number=row_number,
                            reason=reason or type(exc).__name__) from exc

    return len(desired)
