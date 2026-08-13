#!/usr/bin/env python3
"""
Generate a synthetic FlexNet vendor daemon log for demonstration and testing.

Every name in here is invented. The point is a log that exercises each awkward
path in the analyser, so the committed sample report shows what a real one looks
like without shipping anybody's actual usage data:

  * TIMESTAMP lines only every few hours, so dates must be carried as state
  * sessions that span midnight
  * a handful of lines written slightly out of order, as a multi-threaded daemon
    really does, to prove that is not mistaken for midnight
  * a batch host holding hundreds of duplicate handles at once, which is what
    separates the "handles" count from the licence count under DUP_GROUP=HD
  * one host shared by two users
  * long holds left running for days
  * a daemon restart with licences still checked out
  * denials for a feature the licence file does not define
  * a checkin with no matching checkout, because the log starts mid-session

Deterministic for a given --seed, so the sample report is reproducible.

Usage:
    python make_sample_log.py -o sample/sample_vendor.log
    python make_sample_log.py --days 30 --seed 7 -o big.log
"""

from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta

VENDOR = "orcina"
SERVER = "SAMPLE-LICSRV"

# Wholly fictional. Do not replace these with real user or machine names if you
# intend to commit or share the result.
STAFF = [
    ("alice", "ws-alpha"),
    ("bob", "ws-bravo"),
    ("carol", "dell-01"),
    ("dan", "dell-02"),
    ("erin", "ws-echo"),
    ("frank", "ws-foxtrot"),
    ("grace", "dell-03"),
    ("heidi", "ws-hotel"),
]
# One shared compute box, used by two people - the case that makes a per-host
# licence count worth double-checking.
BATCH_HOST = "ws-compute"
BATCH_USERS = ["alice", "frank"]

MAIN_FEATURE = "Flex"
UNLICENSED_FEATURE = "Wave"      # requested by clients, absent from the licence

KEY_BLOB = "00F2 6F5A 8ADE F6BD "
WAVE_BLOB = "PORT_AT_HOST_PLUS   "


def emit_header(out, t):
    """The start-up block a vendor daemon writes, including a failed-start flap."""
    for _ in range(3):
        out.append((t, "SLOG: Summary LOG statistics is enabled."))
        out.append((t, "License server system started on " + SERVER))
        out.append((t, "No features to serve, exiting"))
        out.append((t, "EXITING DUE TO SIGNAL 27 Exit reason 4"))
        t += timedelta(seconds=1)
    out.append((t, "SLOG: Summary LOG statistics is enabled."))
    out.append((t, f"Server started on {SERVER} for:\t{MAIN_FEATURE}\t"))
    out.append((t, "Starting diagnostics output thread (DRQT)"))
    out.append((t, "DRQT: running"))
    return t + timedelta(seconds=2)


def session(out, feature, user, host, start, minutes):
    out.append((start, f'OUT: "{feature}" {user}@{host}  '))
    out.append((start + timedelta(minutes=minutes), f'IN: "{feature}" {user}@{host}  '))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", default="sample/sample_vendor.log")
    ap.add_argument("--days", type=int, default=21, help="days of activity (default: 21)")
    ap.add_argument("--start", default="2025-09-01", help="first date, YYYY-MM-DD")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    day0 = datetime.fromisoformat(args.start).replace(hour=8, minute=41, second=3)
    events: list[tuple[datetime, str]] = []

    t = emit_header(events, day0)

    # A checkin with no preceding checkout: the log began mid-session.
    events.append((t + timedelta(minutes=4), f'IN: "{MAIN_FEATURE}" grace@dell-03  '))

    long_holds = []

    for d in range(args.days):
        base = (day0 + timedelta(days=d)).replace(hour=0, minute=0, second=0)
        weekend = base.weekday() >= 5

        for user, host in STAFF:
            # Not everybody works every day, and weekends are quiet.
            if rng.random() < (0.75 if weekend else 0.12):
                continue
            n = rng.randint(1, 3) if weekend else rng.randint(3, 11)
            for _ in range(n):
                hour = rng.gauss(13, 3.4) if not weekend else rng.gauss(14, 4)
                hour = min(max(hour, 6.0), 21.5)
                start = base + timedelta(hours=hour, minutes=rng.randint(0, 59),
                                         seconds=rng.randint(0, 59))
                # Most checkouts are brief; a minority run for hours.
                r = rng.random()
                if r < 0.55:
                    mins = rng.choice([0, 0, 0, 1, 2])          # near-instant
                elif r < 0.85:
                    mins = rng.randint(3, 90)
                else:
                    mins = rng.randint(120, 600)                # spans evening
                session(events, MAIN_FEATURE, user, host, start, mins)

            # Occasionally somebody leaves a session running for days.
            if rng.random() < 0.06:
                start = base + timedelta(hours=rng.uniform(9, 17))
                hours = rng.uniform(26, 150)
                long_holds.append((start, hours, user, host))
                session(events, MAIN_FEATURE, user, host, start, hours * 60)

        # A parallel batch run: one host takes many handles at once, all of which
        # collapse to a single licence under DUP_GROUP=HD.
        if not weekend and rng.random() < 0.45:
            # Alternate rather than choose at random: the point of this host is to
            # be shared by two users, and leaving that to the RNG means some seeds
            # silently fail to exercise it.
            who = BATCH_USERS[d % len(BATCH_USERS)]
            start = base + timedelta(hours=rng.uniform(9, 16))
            count = rng.choice([40, 80, 120, 190])
            for i in range(count):
                s = start + timedelta(seconds=i * rng.randint(0, 2))
                session(events, MAIN_FEATURE, who, BATCH_HOST, s,
                        rng.randint(1, 25))

        # Clients probing for a feature the licence file does not define. They
        # are refused and carry on; sometimes they then take the main feature.
        if rng.random() < 0.5:
            user, host = rng.choice(STAFF)
            at = base + timedelta(hours=rng.uniform(8, 18))
            for k in range(rng.randint(2, 9)):
                events.append((at + timedelta(seconds=k * 3),
                               f'UNSUPPORTED: "{UNLICENSED_FEATURE}" ({WAVE_BLOB}) '
                               f'{user}@{host}  (No such feature exists. (-5,346))'))
            if rng.random() < 0.3:
                session(events, MAIN_FEATURE, user, host,
                        at + timedelta(seconds=30), rng.randint(5, 120))

        # A client presenting a licence key the daemon does not recognise, then
        # immediately succeeding - a retry, not a shortage.
        if rng.random() < 0.25:
            user, host = rng.choice(STAFF)
            at = base + timedelta(hours=rng.uniform(8, 18))
            events.append((at, f'UNSUPPORTED: "{MAIN_FEATURE}" ({KEY_BLOB})'
                               f' {user}@{host}  (No such feature exists. (-5,346))'))
            session(events, MAIN_FEATURE, user, host, at, rng.randint(2, 60))

    # A daemon restart partway through, with licences still checked out. Their
    # sessions are genuinely released here, and the analyser must close them.
    restart = (day0 + timedelta(days=args.days // 2)).replace(hour=4, minute=12, second=8)
    events.append((restart, "Lost communications with lmgrd. "))
    events.append((restart + timedelta(seconds=1), "EXITING DUE TO SIGNAL 28 Exit reason 5"))
    events.append((restart + timedelta(seconds=25), "SLOG: Summary LOG statistics is enabled."))
    events.append((restart + timedelta(seconds=25),
                   f"Server started on {SERVER} for:\t{MAIN_FEATURE}\t"))

    # Handles still checked out when the daemon dies. No IN: line ever follows:
    # the daemon has gone, and the client reconnects and checks out afresh. The
    # analyser has to close these at the restart instant rather than leave them
    # open, or they become fictitious multi-week sessions.
    for i, (user, host) in enumerate(STAFF[:3]):
        events.append((restart - timedelta(hours=2, minutes=i * 7),
                       f'OUT: "{MAIN_FEATURE}" {user}@{host}  '))

    # Sessions still running when the log was captured, which is the ordinary
    # state of affairs for a live server.
    tail = day0 + timedelta(days=args.days, hours=-4)
    for i, (user, host) in enumerate(STAFF[3:5]):
        events.append((tail + timedelta(minutes=i * 11),
                       f'OUT: "{MAIN_FEATURE}" {user}@{host}  '))

    events.sort(key=lambda e: e[0])

    # Nudge a few lines out of chronological order, exactly as a daemon writing
    # from several threads does. The analyser must not read these as midnight.
    for _ in range(9):
        i = rng.randrange(1, len(events) - 1)
        if abs((events[i][0] - events[i - 1][0]).total_seconds()) < 600:
            events[i], events[i - 1] = events[i - 1], events[i]

    # ---- write, interleaving TIMESTAMP anchors every 6 hours
    import os
    d = os.path.dirname(args.out)
    if d:
        os.makedirs(d, exist_ok=True)

    written = 0
    next_ts = events[0][0].replace(minute=22, second=57)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        def line(dt, msg):
            nonlocal written
            fh.write(f"{dt.hour:2d}:{dt.minute:02d}:{dt.second:02d} ({VENDOR}) {msg}\n")
            written += 1

        for dt, msg in events:
            while dt >= next_ts:
                line(next_ts, f"TIMESTAMP {next_ts.month}/{next_ts.day}/{next_ts.year}")
                next_ts += timedelta(hours=6)
            line(dt, msg)

    size = os.path.getsize(args.out)
    print(f"Wrote {args.out}: {written:,} lines, {size / 1024:.0f} KB, "
          f"{args.days} days from {args.start}")
    print(f"  {len(long_holds)} multi-day holds, seed {args.seed}")
    print("  All user and host names in this file are invented.")


if __name__ == "__main__":
    main()
