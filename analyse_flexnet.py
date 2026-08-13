#!/usr/bin/env python3
"""
Parse a FlexNet Publisher vendor daemon (lmgrd/vendord) debug log and produce
licence-capacity statistics as JSON.

Two things about this log format drive the design.

1. Individual event lines carry only HH:MM:SS, no date. Dates appear solely on
   periodic "TIMESTAMP m/d/yyyy" lines. So the date is carried forward as parser
   state, with midnight inferred when the clock jumps backwards by more than
   ROLLOVER_THRESHOLD_SECS -- not on *any* backwards step, because the daemon
   writes from several threads and small backwards steps occur in a perfectly
   normal log. Each TIMESTAMP is then a hard re-anchor, and disagreement between
   the walked date and the anchor is counted and reported, not swallowed.

2. Concurrency has several defensible definitions, and which one is the real
   licence count depends on the DUP_GROUP setting in the licence file:
      hosts   - distinct host holding >=1 handle (DUP_GROUP=HD)  <- authoritative
      seats   - distinct user@host                (DUP_GROUP=UH)
      users   - distinct user, any host           (DUP_GROUP=USER)
      handles - every OUT counts                  (DUP_GROUP=NONE)
   All four are computed so the sensitivity is visible. Reporting only "handles"
   is how one parallel batch job gets mistaken for organisational demand.

Usage:
    python analyse_flexnet.py orcina.log [-o usage_data.json] [--vendor orcina]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict, deque
from datetime import date, datetime, timedelta

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------

BUSINESS_START_HOUR = 8           # inclusive
BUSINESS_END_HOUR = 18            # exclusive
BUSINESS_DAYS = {0, 1, 2, 3, 4}   # Mon-Fri (date.weekday())

# A backwards step in the clock is read as midnight only if it exceeds this.
# This log contains a 16:22:25 line sitting after a 16:26:24 one; treating that
# as midnight invents extra days and produces sessions that end before they
# start. Because TIMESTAMP anchors land every few hours, a genuine unanchored
# midnight crossing always presents as a step of far more than 12 hours.
ROLLOVER_THRESHOLD_SECS = 12 * 3600

# Usernames appear with inconsistent capitalisation for the same person
# ('ian'/'Ian', 'jamesp'/'JamesP'), which would otherwise count one person as
# two seats.
CASEFOLD_IDENTITIES = True

# Orcina licences always carry DUP_GROUP=HD, so concurrent checkouts from one
# host consume a single licence, and counting distinct hosts is the only rule
# that corresponds to what is actually licensed. Other groupings are deliberately
# not computed: offering them would invite sizing against a number that does not
# apply. The statistics stay keyed by mode so the shape is easy to extend if that
# ever changes.
MODES = ("hosts",)
PRIMARY_MODE = "hosts"

MODE_META = {
    "hosts": {
        "label": "Distinct hosts",
        "dup_group": "DUP_GROUP=HD",
        "authoritative": True,
        "note": "The display is not recorded in the log, so this assumes one "
                "display per host - true for a workstation, though a terminal "
                "server or Citrix host consumes one licence per concurrent "
                "session, which would make these figures a lower bound.",
    },
}

ANY_FEATURE = "(any feature)"

# Session-duration histogram edges, in minutes. None = open-ended.
DURATION_BINS = [
    ("< 1 min", 0, 1),
    ("1-5 min", 1, 5),
    ("5-15 min", 5, 15),
    ("15-30 min", 15, 30),
    ("30-60 min", 30, 60),
    ("1-2 h", 60, 120),
    ("2-4 h", 120, 240),
    ("4-8 h", 240, 480),
    ("8-24 h", 480, 1440),
    ("> 24 h", 1440, None),
]

# Features with fewer sessions than this stay in the tables but are left out of
# the charted series (avoids a flat line burning a palette slot).
CHART_MIN_SESSIONS = 50

# The chart palette seats three categorical series plus the aggregate while
# staying inside its colour-blind separation gates. Any further features remain
# in the per-feature table, and the report states how many were left out.
MAX_CHARTED_FEATURES = 3

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# --------------------------------------------------------------------------
# Line grammar
# --------------------------------------------------------------------------

def build_patterns(vendor: str):
    v = re.escape(vendor)
    return {
        # Leading hour is space-padded for single digits: " 4:27:57"
        "line": re.compile(r"^\s*(\d{1,2}):(\d{2}):(\d{2}) \(" + v + r"\) (.*?)\s*$"),
        "ts": re.compile(r"^TIMESTAMP (\d{1,2})/(\d{1,2})/(\d{4})"),
        # OUT/IN may carry a licence-key blob in parentheses before user@host.
        "ckt": re.compile(r'^(OUT|IN): "([^"]*)" (?:\([^)]*\) )?([^@\s]+)@(\S+)'),
        "denial": re.compile(
            r'^(UNSUPPORTED|DENIED|NOTLICENSED): "([^"]*)" (?:\([^)]*\) )?'
            r"([^@\s]+)@(\S+)\s*\((.*)\)\s*$"
        ),
        # Any of these mean every handle the daemon held is gone.
        "reset": re.compile(
            r"^(Server started on .* for:|License server system started"
            r"|EXITING|Exiting\.|Lost communications with lmgrd)"
        ),
        "checkin_failed": re.compile(r'^Checkin failed feature "([^"]*)": (\S+)'),
    }


# --------------------------------------------------------------------------
# Pass 1 - tokenise
# --------------------------------------------------------------------------

class Ev:
    """One log event, before a date has been attached."""
    __slots__ = ("kind", "secs", "feature", "user", "host", "extra", "day")

    def __init__(self, kind, secs, feature=None, user=None, host=None, extra=None):
        self.kind = kind          # OUT | IN | DENY | TS | RESET | CKFAIL
        self.secs = secs          # seconds since midnight
        self.feature = feature
        self.user = user
        self.host = host
        self.extra = extra
        self.day = None           # date, filled in by pass 2


def norm(s: str) -> str:
    return s.lower() if (CASEFOLD_IDENTITIES and s) else s


def tokenise(path: str, vendor: str):
    pat = build_patterns(vendor)
    events: list[Ev] = []
    stats = Counter()

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            stats["lines"] += 1
            m = pat["line"].match(raw)
            if not m:
                stats["unparsed_lines"] += 1
                continue
            secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            msg = m.group(4)

            mt = pat["ts"].match(msg)
            if mt:
                mo, dy, yr = (int(x) for x in mt.groups())
                events.append(Ev("TS", secs, extra=date(yr, mo, dy)))
                stats["timestamp_lines"] += 1
                continue

            mc = pat["ckt"].match(msg)
            if mc:
                kind, feat, user, host = mc.groups()
                events.append(Ev(kind, secs, feat, norm(user), norm(host)))
                stats["checkout_lines" if kind == "OUT" else "checkin_lines"] += 1
                continue

            md = pat["denial"].match(msg)
            if md:
                kind, feat, user, host, reason = md.groups()
                events.append(Ev("DENY", secs, feat, norm(user), norm(host),
                                 extra={"kind": kind, "reason": reason.strip()}))
                stats["denial_lines"] += 1
                continue

            mf = pat["checkin_failed"].match(msg)
            if mf:
                events.append(Ev("CKFAIL", secs, mf.group(1), norm(mf.group(2))))
                stats["checkin_failed_lines"] += 1
                continue

            if pat["reset"].match(msg):
                events.append(Ev("RESET", secs, extra=msg))
                stats["daemon_reset_lines"] += 1
                continue

            stats["ignored_lines"] += 1

    return events, stats


# --------------------------------------------------------------------------
# Pass 2 - attach dates
# --------------------------------------------------------------------------

def assign_dates(events: list[Ev]) -> dict:
    diag = Counter()
    first_ts = next((i for i, e in enumerate(events) if e.kind == "TS"), None)
    if first_ts is None:
        raise SystemExit("No TIMESTAMP lines found - cannot date this log. "
                         "Enable TIMESTAMP logging on the vendor daemon.")

    # --- backfill the prefix, walking backwards from the first anchor.
    # Walking back, a clock that reads *later* than its successor by more than
    # the threshold means we stepped back over midnight.
    cur = events[first_ts].extra
    prev = events[first_ts].secs
    for e in reversed(events[:first_ts]):
        if e.secs - prev > ROLLOVER_THRESHOLD_SECS:
            cur -= timedelta(days=1)
            diag["rollovers_backward"] += 1
            prev = e.secs
        elif e.secs > prev:
            diag["out_of_order_lines"] += 1     # thread jitter; hold the mark
        else:
            prev = e.secs
        e.day = cur

    # --- walk forward from the first anchor, re-anchoring at each TIMESTAMP
    cur = events[first_ts].extra
    prev = events[first_ts].secs
    events[first_ts].day = cur
    for e in events[first_ts + 1:]:
        if prev - e.secs > ROLLOVER_THRESHOLD_SECS:
            cur += timedelta(days=1)
            diag["rollovers_forward"] += 1
            prev = e.secs
        elif e.secs < prev:
            diag["out_of_order_lines"] += 1     # thread jitter; hold the mark
        else:
            prev = e.secs

        if e.kind == "TS":
            if cur != e.extra:
                # Trust the anchor. A mismatch means the daemon was down or
                # idle long enough for a rollover to pass undetected, or the
                # host clock was adjusted.
                diag["anchor_corrections"] += 1
                diag["anchor_drift_days"] += abs((e.extra - cur).days)
                cur = e.extra
                prev = e.secs
        e.day = cur

    return dict(diag)


def to_dt(e: Ev) -> datetime:
    return datetime.combine(e.day, datetime.min.time()) + timedelta(seconds=e.secs)


# --------------------------------------------------------------------------
# Pass 3 - pair checkouts into sessions
# --------------------------------------------------------------------------

class Session:
    __slots__ = ("feature", "user", "host", "start", "end", "flag")

    def __init__(self, feature, user, host, start, end, flag):
        self.feature = feature
        self.user = user
        self.host = host
        self.start = start
        self.end = end
        self.flag = flag       # closed | truncated | open

    @property
    def seconds(self):
        return (self.end - self.start).total_seconds()


def build_sessions(events: list[Ev]):
    """FIFO-pair OUT with IN per (feature, user, host).

    An 'IN:' line does not reference the 'OUT:' it closes, so pairing is
    necessarily heuristic. FIFO is the right heuristic: it closes the oldest
    outstanding handle, which keeps durations meaningful.

    On a daemon restart every outstanding handle is genuinely released, so open
    sessions are closed at the restart instant and flagged 'truncated' rather
    than discarded -- the licence really was held up to that point. Leaving them
    open would manufacture multi-week sessions and wildly inflate the peaks.
    """
    open_h: dict[tuple, deque] = defaultdict(deque)
    sessions: list[Session] = []
    diag = Counter()
    resets = []

    for e in events:
        if e.kind == "OUT":
            open_h[(e.feature, e.user, e.host)].append(to_dt(e))
            diag["checkouts"] += 1
        elif e.kind == "IN":
            q = open_h[(e.feature, e.user, e.host)]
            if q:
                sessions.append(Session(e.feature, e.user, e.host,
                                        q.popleft(), to_dt(e), "closed"))
            else:
                diag["orphan_checkins"] += 1   # log began mid-session
            diag["checkins"] += 1
        elif e.kind == "RESET":
            at = to_dt(e)
            resets.append({"at": at.isoformat(sep=" "), "msg": e.extra})
            n = 0
            for key, q in open_h.items():
                while q:
                    sessions.append(Session(key[0], key[1], key[2],
                                            q.popleft(), at, "truncated"))
                    n += 1
            if n:
                diag["truncated_at_reset"] += n

    log_end = max((to_dt(e) for e in events), default=None)
    for key, q in open_h.items():
        while q:
            sessions.append(Session(key[0], key[1], key[2],
                                    q.popleft(), log_end, "open"))
            diag["open_at_log_end"] += 1

    # A session cannot end before it starts. Any that do are a canary on the
    # date state machine, so they are counted loudly and clamped, not dropped.
    for s in sessions:
        if s.end < s.start:
            diag["negative_duration_clamped"] += 1
            s.end = s.start

    sessions.sort(key=lambda s: s.start)
    return sessions, dict(diag), resets


# --------------------------------------------------------------------------
# Interval helpers
# --------------------------------------------------------------------------

def business_seconds(t0: datetime, t1: datetime) -> float:
    """Seconds of [t0, t1) falling inside business hours."""
    if t1 <= t0:
        return 0.0
    total = 0.0
    day = t0.date()
    last = t1.date()
    while day <= last:
        if day.weekday() in BUSINESS_DAYS:
            base = datetime.combine(day, datetime.min.time())
            lo = max(t0, base + timedelta(hours=BUSINESS_START_HOUR))
            hi = min(t1, base + timedelta(hours=BUSINESS_END_HOUR))
            if hi > lo:
                total += (hi - lo).total_seconds()
        day += timedelta(days=1)
    return total


def business_label():
    days = sorted(BUSINESS_DAYS)
    names = [DAY_NAMES[d].capitalize() for d in days]
    contiguous = len(days) > 1 and days == list(range(days[0], days[-1] + 1))
    span = f"{names[0]}-{names[-1]}" if contiguous else ",".join(names)
    return f"{span} {BUSINESS_START_HOUR:02d}:00-{BUSINESS_END_HOUR:02d}:00"


def hour_chunks(t0: datetime, t1: datetime):
    """Split [t0, t1) on hour boundaries, yielding (weekday, hour, seconds)."""
    cur = t0
    while cur < t1:
        nxt = (cur + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        if nxt > t1:
            nxt = t1
        yield cur.weekday(), cur.hour, (nxt - cur).total_seconds()
        cur = nxt


def merge_intervals(ivs):
    """Union a list of (start, end) tuples."""
    if not ivs:
        return []
    ivs = sorted(ivs)
    out = [list(ivs[0])]
    for s, e in ivs[1:]:
        if s <= out[-1][1]:
            if e > out[-1][1]:
                out[-1][1] = e
        else:
            out.append([s, e])
    return [tuple(x) for x in out]


def interval_sets(sessions):
    """{feature: {"hosts": [(start, end), ...]}}.

    A host's overlapping handles are unioned into one interval, so a machine
    holding six handles at once counts as one licence for as long as it holds at
    least one of them. That union is the whole point: it is what DUP_GROUP=HD
    does, and skipping it would count a parallel batch run many times over.
    """
    host_ivs = defaultdict(lambda: defaultdict(list))
    for s in sessions:
        iv = (s.start, s.end)
        for f in (s.feature, ANY_FEATURE):
            host_ivs[f][s.host].append(iv)

    return {f: {"hosts": [iv for ivs in hosts.values()
                          for iv in merge_intervals(ivs)]}
            for f, hosts in host_ivs.items()}


# --------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------

def sweep(intervals, window, biz_only: bool):
    """Time-weighted concurrency sweep over [window[0], window[1]).

    Returns (time_at_level, peak, peak_at). At an identical instant a release is
    processed before an acquire: FlexNet logs to 1-second resolution, and a
    checkin and checkout in the same logged second is almost always one handle
    being handed over. Counting the acquire first would invent a peak that never
    existed. The convention has to be stated because it cannot be derived.
    """
    w0, w1 = window
    deltas = []
    for s, e in intervals:
        s = max(s, w0)
        e = min(e, w1)
        if e >= s:
            deltas.append((s, 1, 1))     # sort key 1 -> acquire second
            deltas.append((e, 0, -1))    # sort key 0 -> release first
    if not deltas:
        return Counter(), 0, None

    deltas.sort(key=lambda x: (x[0], x[1]))

    time_at = Counter()
    level = peak = 0
    peak_at = None
    # Start at the window edge, not the first event: the quiet stretch before
    # anything is checked out is still observed time at a concurrency of zero,
    # and excluding it shrinks every denominator computed from this sweep.
    prev_t = w0

    for t, _order, d in deltas:
        if t > prev_t:
            dur = business_seconds(prev_t, t) if biz_only else (t - prev_t).total_seconds()
            if dur > 0:
                time_at[level] += dur
            prev_t = t
        level += d
        if level > peak:
            peak, peak_at = level, t

    # And the quiet stretch after the last release, for the same reason.
    if w1 > prev_t:
        dur = business_seconds(prev_t, w1) if biz_only else (w1 - prev_t).total_seconds()
        if dur > 0:
            time_at[level] += dur

    return time_at, peak, peak_at


def quantile_from_time_at(time_at: Counter, q: float):
    total = sum(time_at.values())
    if total <= 0:
        return 0
    target = total * q
    run = 0.0
    for lvl in sorted(time_at):
        run += time_at[lvl]
        if run >= target:
            return lvl
    return max(time_at)


def exceedance(time_at: Counter):
    """[{n, pct}] -- percentage of observed time at or above n. The sizing curve."""
    total = sum(time_at.values())
    if total <= 0:
        return []
    top = max(time_at)
    cum = 0.0
    out = [None] * (top + 1)
    for n in range(top, -1, -1):
        cum += time_at.get(n, 0.0)
        out[n] = {"n": n, "pct": round(100.0 * cum / total, 4)}
    return out


def stats_for(intervals, window, biz_only):
    time_at, peak, peak_at = sweep(intervals, window, biz_only)
    total = sum(time_at.values())
    held = sum(lvl * sec for lvl, sec in time_at.items())
    return {
        "peak": peak,
        "peak_at": peak_at.isoformat(sep=" ") if peak_at else None,
        "p50": quantile_from_time_at(time_at, 0.50),
        "p95": quantile_from_time_at(time_at, 0.95),
        "p99": quantile_from_time_at(time_at, 0.99),
        "p999": quantile_from_time_at(time_at, 0.999),
        "mean": round(held / total, 3) if total else 0.0,
        "licence_hours": round(held / 3600.0, 1),
        "pct_time_idle": round(100.0 * time_at.get(0, 0.0) / total, 2) if total else 100.0,
        "observed_hours": round(total / 3600.0, 1),
        "exceedance": exceedance(time_at),
    }


def worst_duplicate_holder(sessions, feature, window):
    """Largest number of handles one user@host held at once for this feature.

    This is the number that explains an implausible 'handles' peak.
    """
    by_host = defaultdict(list)
    for s in sessions:
        if s.feature == feature:
            by_host[s.host].append((s.start, s.end))
    best, who = 0, None
    for host, ivs in by_host.items():
        _t, pk, _at = sweep(ivs, window, False)
        if pk > best:
            best, who = pk, host
    return {"peak_handles": best, "host": who}


def analyse_slice(sessions, window, biz_only):
    sets = interval_sets(sessions)
    w0, w1 = window
    span = business_seconds(w0, w1) if biz_only else (w1 - w0).total_seconds()

    feats = {}
    for feat, modes in sets.items():
        entry = {m: stats_for(modes[m], window, biz_only) for m in MODES}
        if feat != ANY_FEATURE:
            entry["duplicate"] = worst_duplicate_holder(sessions, feat, window)
        feats[feat] = entry
    return {"span_hours": round(span / 3600.0, 1), "features": feats}


# --------------------------------------------------------------------------
# Per-day, heatmap, durations, users
# --------------------------------------------------------------------------

def daily_series(sessions, features, window):
    w0, w1 = window
    days = []
    d = w0.date()
    while d <= w1.date():
        days.append(d)
        d += timedelta(days=1)

    sets = interval_sets(sessions)
    out = {"days": [d.isoformat() for d in days], "series": {}}
    for feat in features:
        modes = sets.get(feat)
        if not modes:
            continue
        out["series"][feat] = {}
        for mode in MODES:
            peaks, p95s = [], []
            for d in days:
                d0 = datetime.combine(d, datetime.min.time())
                d1 = d0 + timedelta(days=1)
                if d1 <= w0 or d0 >= w1:
                    peaks.append(None)
                    p95s.append(None)
                    continue
                time_at, peak, _ = sweep(modes[mode], (max(d0, w0), min(d1, w1)), False)
                peaks.append(peak)
                p95s.append(quantile_from_time_at(time_at, 0.95) if time_at else 0)
            out["series"][feat][mode] = {"peak": peaks, "p95": p95s}
    return out


def heatmap(intervals, window):
    """Mean and peak concurrency by (weekday, hour)."""
    w0, w1 = window
    deltas = []
    for s, e in intervals:
        s, e = max(s, w0), min(e, w1)
        if e > s:
            deltas.append((s, 1, 1))
            deltas.append((e, 0, -1))

    weighted = defaultdict(float)
    peak = defaultdict(int)
    if deltas:
        deltas.sort(key=lambda x: (x[0], x[1]))
        level = 0
        prev_t = deltas[0][0]
        for t, _o, d in deltas:
            if t > prev_t:
                for dow, hr, sec in hour_chunks(prev_t, t):
                    weighted[(dow, hr)] += level * sec
                    if level > peak[(dow, hr)]:
                        peak[(dow, hr)] = level
                prev_t = t
            level += d

    # Divide by wall-clock seconds in the window, so an hour with no events
    # reads as mean concurrency zero rather than going missing.
    wall = defaultdict(float)
    for dow, hr, sec in hour_chunks(w0, w1):
        wall[(dow, hr)] += sec

    cells = []
    for dow in range(7):
        for hr in range(24):
            w = wall[(dow, hr)]
            cells.append({
                "dow": dow, "hour": hr,
                "mean": round(weighted[(dow, hr)] / w, 3) if w > 0 else None,
                "peak": peak[(dow, hr)] if w > 0 else None,
            })
    return cells


def duration_histogram(sessions, feature=None):
    counts = [0] * len(DURATION_BINS)
    for s in sessions:
        if feature and s.feature != feature:
            continue
        mins = s.seconds / 60.0
        for i, (_lab, lo, hi) in enumerate(DURATION_BINS):
            if mins >= lo and (hi is None or mins < hi):
                counts[i] += 1
                break
    return [{"label": lab, "count": c} for (lab, _l, _h), c in zip(DURATION_BINS, counts)]


def per_user(sessions):
    agg = defaultdict(lambda: {"sessions": 0, "hours": 0.0, "features": Counter(),
                               "hosts": set(), "longest_h": 0.0, "peak_dup": 0})
    by_seat_feat = defaultdict(list)
    for s in sessions:
        a = agg[s.user]
        a["sessions"] += 1
        a["hours"] += s.seconds / 3600.0
        a["features"][s.feature] += 1
        a["hosts"].add(s.host)
        a["longest_h"] = max(a["longest_h"], s.seconds / 3600.0)
        by_seat_feat[(s.user, s.feature)].append((s.start, s.end))

    for (user, _feat), ivs in by_seat_feat.items():
        _t, pk, _at = sweep(ivs, (min(i[0] for i in ivs), max(i[1] for i in ivs)), False)
        if pk > agg[user]["peak_dup"]:
            agg[user]["peak_dup"] = pk

    out = [{
        "user": user,
        "sessions": a["sessions"],
        "handle_hours": round(a["hours"], 1),
        "hosts": len(a["hosts"]),
        "longest_session_h": round(a["longest_h"], 1),
        "peak_concurrent_handles": a["peak_dup"],
        "features": dict(a["features"]),
    } for user, a in agg.items()]
    out.sort(key=lambda r: -r["handle_hours"])
    return out


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def parse_hours(text):
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", text or "")
    if not m:
        raise argparse.ArgumentTypeError("expected START-END in whole hours, e.g. 8-18")
    a, b = int(m.group(1)), int(m.group(2))
    if not 0 <= a < b <= 24:
        raise argparse.ArgumentTypeError("need 0 <= START < END <= 24")
    return a, b


def parse_days(text):
    out = []
    for tok in (text or "").split(","):
        tok = tok.strip().lower()[:3]
        if not tok:
            continue
        if tok not in DAY_NAMES:
            raise argparse.ArgumentTypeError(f"unknown day '{tok}' (use mon..sun)")
        out.append(DAY_NAMES.index(tok))
    if not out:
        raise argparse.ArgumentTypeError("no days given")
    return sorted(set(out))


def main():
    ap = argparse.ArgumentParser(
        description="Analyse an Orcina FlexNet vendor daemon log for licence "
                    "capacity, writing JSON for build_report.py to render.",
        epilog="The log must contain TIMESTAMP lines or events cannot be dated. "
               "Add 'TIMESTAMP 1' to the vendor options file if they are missing.")
    ap.add_argument("log", help="path to the vendor daemon log")
    ap.add_argument("-o", "--out", default="usage_data.json",
                    help="output JSON path (default: usage_data.json)")
    ap.add_argument("--vendor", default="orcina",
                    help="name in the '(vendor)' tag on each line (default: orcina)")
    ap.add_argument("--business-hours", type=parse_hours, default=(8, 18),
                    metavar="START-END",
                    help="business-hours window in whole hours (default: 8-18)")
    ap.add_argument("--business-days", type=parse_days, default=[0, 1, 2, 3, 4],
                    metavar="DAYS",
                    help="comma-separated business days (default: mon,tue,wed,thu,fri)")
    ap.add_argument("--min-sessions", type=int, default=CHART_MIN_SESSIONS,
                    metavar="N",
                    help=f"features with fewer sessions are left out of the charts "
                         f"but kept in the tables (default: {CHART_MIN_SESSIONS})")
    ap.add_argument("--expected-denials", default="Wave", metavar="FEATURES",
                    help="comma-separated features whose denials are expected and "
                         "harmless (default: Wave, which OrcaWave probes for before "
                         "falling back to Flex)")
    ap.add_argument("--no-casefold", action="store_true",
                    help="treat 'Ian' and 'ian' as two different users; by default "
                         "identities are case-folded, since one person often appears "
                         "spelled both ways")
    ap.add_argument("--title", default=None,
                    help="heading for the report (default: derived from the vendor)")
    args = ap.parse_args()

    # Module-level so the interval helpers can read them without a config object
    # being threaded through every function.
    global BUSINESS_START_HOUR, BUSINESS_END_HOUR, BUSINESS_DAYS, CASEFOLD_IDENTITIES
    BUSINESS_START_HOUR, BUSINESS_END_HOUR = args.business_hours
    BUSINESS_DAYS = set(args.business_days)
    CASEFOLD_IDENTITIES = not args.no_casefold

    print(f"Tokenising {args.log} ...", file=sys.stderr)
    events, line_stats = tokenise(args.log, args.vendor)
    if not any(e.kind in ("OUT", "IN") for e in events):
        raise SystemExit(
            f"No checkout/checkin events found for vendor '{args.vendor}' in "
            f"{args.log}.\nEither --vendor does not match the '(vendor)' tag on "
            f"each line, or this is an lmgrd log rather than a vendor daemon log.")

    date_diag = assign_dates(events)
    sessions, sess_diag, resets = build_sessions(events)
    print(f"  {len(events):,} events -> {len(sessions):,} sessions", file=sys.stderr)
    if not sessions:
        raise SystemExit(
            f"{args.log} contains checkout or checkin lines, but none of them "
            f"could be paired into a session.\nThat normally means the log holds "
            f"only IN: lines -- it begins after every checkout it records -- so "
            f"there is no held duration to measure. Collect a period that starts "
            f"before the checkouts you care about.")
    if len(sessions) < 25:
        print(f"  NOTE: only {len(sessions)} session(s) here. The report will be "
              f"thin, and percentiles\n        and the sizing curve need weeks of "
              f"real activity before they mean much.", file=sys.stderr)

    first_dt, last_dt = to_dt(events[0]), to_dt(events[-1])
    window_all = (first_dt, last_dt)

    feat_counts = Counter(s.feature for s in sessions)
    all_features = [f for f, _ in feat_counts.most_common()]
    eligible = [f for f in all_features if feat_counts[f] >= args.min_sessions]
    # Falling back to all_features matters on a quiet server: with nothing over
    # the threshold there would be no charted series at all, and every chart
    # would render empty rather than merely sparse.
    charted = (eligible or all_features)[:MAX_CHARTED_FEATURES]
    omitted = [f for f in all_features if f not in charted]

    # Features that only ever appear in denials never reach a session, so they
    # would vanish from the report entirely without this.
    denied_features = sorted({e.feature for e in events if e.kind == "DENY"}
                             - set(all_features))

    ranges = {
        "all": window_all,
        "d30": (max(first_dt, last_dt - timedelta(days=30)), last_dt),
        "d7": (max(first_dt, last_dt - timedelta(days=7)), last_dt),
    }
    clipped = {r: [s for s in sessions if s.end > w[0] and s.start < w[1]]
               for r, w in ranges.items()}

    slices = {}
    for rname, win in ranges.items():
        for scope, biz in (("all", False), ("biz", True)):
            print(f"  slice {rname}/{scope} ...", file=sys.stderr)
            slices[f"{rname}|{scope}"] = analyse_slice(clipped[rname], win, biz)

    print("  daily series ...", file=sys.stderr)
    daily = {r: daily_series(clipped[r], charted + [ANY_FEATURE], ranges[r])
             for r in ranges}

    print("  heatmaps ...", file=sys.stderr)
    heat_feature = charted[0] if charted else None
    heat = {}
    for r, w in ranges.items():
        sets = interval_sets(clipped[r])
        # A window can hold no sessions whatsoever -- an idle server, or a log
        # whose activity all predates the last 7 days -- and interval_sets then
        # has no entry for the feature. Empty cells, not a traceback.
        modes = sets.get(heat_feature) if heat_feature else None
        heat[r] = {m: (heatmap(modes[m], w) if modes else []) for m in MODES}

    durations = {r: {f: duration_histogram(clipped[r], f) for f in charted}
                 for r in ranges}
    users = {r: per_user(clipped[r]) for r in ranges}

    # UNSUPPORTED / "No such feature exists" is a client asking for something
    # the licence file does not contain -- NOT a capacity denial. They are
    # separated because conflating the two is how you buy licences you do not
    # need.
    denials = [e for e in events if e.kind == "DENY"]
    capacity_re = re.compile(r"already reached|Licensed number of users|users? (?:are )?"
                             r"already|max.*reached", re.I)

    # What happened in the seconds after each refusal? If the same user is
    # granted something straight afterwards, the refusal was a client probing
    # for a feature or licence key it does not have, and nobody was blocked.
    FOLLOW_SECS = 5
    follow = Counter()
    for i, e in enumerate(events):
        if e.kind != "DENY":
            continue
        t0 = to_dt(e)
        outcome = f"nothing granted within {FOLLOW_SECS}s"
        for f in events[i + 1: i + 60]:
            if f.kind not in ("OUT", "IN", "DENY"):
                continue
            if (to_dt(f) - t0).total_seconds() > FOLLOW_SECS:
                break
            if f.kind == "OUT" and f.user == e.user:
                outcome = ("same feature granted" if f.feature == e.feature
                           else f"fell back to {f.feature}")
                break
        follow[(e.feature, outcome)] += 1

    den_summary = {
        "total": len(denials),
        "capacity": sum(1 for e in denials if capacity_re.search(e.extra["reason"])),
        "follow_window_secs": FOLLOW_SECS,
        "by_reason": [{"reason": r, "count": c} for r, c in
                      Counter(e.extra["reason"] for e in denials).most_common()],
        "by_feature": [{"feature": f, "count": c} for f, c in
                       Counter(e.feature for e in denials).most_common()],
        "by_user": [{"user": u, "count": c} for u, c in
                    Counter(e.user for e in denials).most_common()],
        "by_host": [{"host": h, "count": c} for h, c in
                    Counter(e.host for e in denials).most_common()],
        "by_seat": [{"seat": s, "count": c} for s, c in
                    Counter(f"{e.user}@{e.host}" for e in denials).most_common()],
        "by_day": [{"day": d, "count": c} for d, c in
                   sorted(Counter(e.day.isoformat() for e in denials).items())],
        "aftermath": [{"feature": f, "outcome": o, "count": c}
                      for (f, o), c in follow.most_common()],
        "denial_only_features": denied_features,
        "expected_features": [f.strip() for f in args.expected_denials.split(",")
                              if f.strip()],
    }

    # Does any host serve more than one user? If so the per-host counting rule
    # deserves a second look, and the report should name them.
    users_per_host = defaultdict(set)
    for s in sessions:
        users_per_host[s.host].add(s.user)
    shared_hosts = sorted(h for h, u in users_per_host.items() if len(u) > 1)

    payload = {
        "meta": {
            "log": args.log,
            "vendor": args.vendor,
            "title": args.title or f"Licence capacity - FlexNet vendor daemon",
            "tool_version": "1.0.0",
            "generated": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "first_event": first_dt.isoformat(sep=" "),
            "last_event": last_dt.isoformat(sep=" "),
            "span_days": round((last_dt - first_dt).total_seconds() / 86400.0, 1),
            "business_hours": business_label(),
            "line_stats": dict(line_stats),
            "date_diagnostics": date_diag,
            "session_diagnostics": sess_diag,
            "features": [{"feature": f, "sessions": feat_counts[f]} for f in all_features],
            "charted_features": charted,
            "omitted_features": omitted,
            "min_sessions": args.min_sessions,
            "heat_feature": heat_feature,
            "modes": list(MODES),
            "mode_meta": MODE_META,
            "primary_mode": PRIMARY_MODE,
            "dup_group": "HD",
            "any_feature_label": ANY_FEATURE,
            "casefolded": CASEFOLD_IDENTITIES,
            "shared_hosts": shared_hosts,
            "unique_users": len({s.user for s in sessions}),
            "unique_hosts": len({s.host for s in sessions}),
            "unique_seats": len({(s.user, s.host) for s in sessions}),
            "daemon_restarts": resets,
            "duration_bins": [b[0] for b in DURATION_BINS],
            "rollover_threshold_hours": ROLLOVER_THRESHOLD_SECS // 3600,
        },
        "slices": slices,
        "daily": daily,
        "heatmap": heat,
        "durations": durations,
        "users": users,
        "denials": den_summary,
        "longest_sessions": [{
            "feature": s.feature, "user": s.user, "host": s.host,
            "start": s.start.isoformat(sep=" "), "end": s.end.isoformat(sep=" "),
            "hours": round(s.seconds / 3600.0, 1), "flag": s.flag,
        } for s in sorted(sessions, key=lambda s: -s.seconds)[:20]],
    }

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    print(f"Wrote {args.out}", file=sys.stderr)

    # ---- console summary
    m = payload["meta"]
    print("\n" + "=" * 72)
    print(f"  {m['first_event']}  ->  {m['last_event']}   ({m['span_days']} days)")
    print("=" * 72)
    print(f"  lines {m['line_stats'].get('lines', 0):,}   "
          f"checkouts {m['line_stats'].get('checkout_lines', 0):,}   "
          f"checkins {m['line_stats'].get('checkin_lines', 0):,}")
    print(f"  users {m['unique_users']}   hosts {m['unique_hosts']}   "
          f"seats {m['unique_seats']}   sessions {len(sessions):,}")
    print(f"  date diagnostics:    {m['date_diagnostics']}")
    print(f"  session diagnostics: {m['session_diagnostics']}")
    if omitted:
        print(f"  charted: {', '.join(charted) or 'none'}"
              f"   (not charted: {', '.join(omitted)})")
    if shared_hosts:
        print(f"  hosts used by more than one user: {', '.join(shared_hosts)}")
    print("\n  --- concurrent licences in use, DUP_GROUP=HD "
          "(whole period, all hours) ---")
    rows = sorted(slices["all|all"]["features"].items(),
                  key=lambda kv: -kv[1][PRIMARY_MODE]["peak"])
    for feat, st in rows:
        s = st[PRIMARY_MODE]
        dup = st.get("duplicate") or {}
        extra = (f"  max {dup['peak_handles']} handles on {dup['host']}"
                 if dup.get("peak_handles", 0) > 1 else "")
        print(f"    {feat:<16} peak {s['peak']:>4}  p99 {s['p99']:>4}  "
              f"p95 {s['p95']:>4}  mean {s['mean']:>7}  "
              f"idle {s['pct_time_idle']:>5}%{extra}")
    print(f"\n  Denials: {den_summary['total']} total, "
          f"{den_summary['capacity']} capacity-related")
    if denied_features:
        print(f"  Features seen only in denials: {', '.join(denied_features)}")
    print()


if __name__ == "__main__":
    main()
