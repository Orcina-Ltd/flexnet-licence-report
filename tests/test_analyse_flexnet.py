#!/usr/bin/env python3
"""
Tests for analyse_flexnet.py. Standard library only, to match the tool.

    python -m unittest discover -s tests -v

Each test builds the log it needs inline, so the input sits next to the
assertion about it. Where a test exists because something actually broke, the
docstring says so -- those are the ones not to delete.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import analyse_flexnet as afx          # noqa: E402

VENDOR = "orcina"
ANALYSER = os.path.join(ROOT, "analyse_flexnet.py")
BUILDER = os.path.join(ROOT, "build_report.py")


def line(hms, msg):
    """One log line. hms is the literal time field, e.g. '14:37:41' or ' 4:07:01'."""
    return f"{hms} ({VENDOR}) {msg}"


def out(hms, feature, user, host):
    return line(hms, f'OUT: "{feature}" {user}@{host}  ')


def inn(hms, feature, user, host):
    return line(hms, f'IN: "{feature}" {user}@{host}  ')


def ts(hms, y, m, d):
    return line(hms, f"TIMESTAMP {m}/{d}/{y}")


BASE = datetime(2026, 3, 2, 0, 0, 0)      # a Monday


def dt(hours=0, minutes=0, days=0, seconds=0):
    return BASE + timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def session(feature="Flex", user="alice", host="ws-a", start=None, end=None):
    return afx.Session(feature, user, host,
                       start if start else dt(9),
                       end if end else dt(10), "closed")


class Base(unittest.TestCase):
    def setUp(self):
        # main() sets these from argv; unit tests call the internals directly, so
        # restore the documented defaults rather than inherit another test's.
        afx.BUSINESS_START_HOUR = 8
        afx.BUSINESS_END_HOUR = 18
        afx.BUSINESS_DAYS = {0, 1, 2, 3, 4}
        afx.CASEFOLD_IDENTITIES = True
        afx.ROLLOVER_THRESHOLD_SECS = 12 * 3600
        self.tmp = tempfile.mkdtemp(prefix="fnp-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, lines, newline="\n", name="vendor.log"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(newline.join(lines) + newline)
        return path

    def parse(self, lines, **kw):
        """Tokenise and date a log, returning (events, stats, diagnostics)."""
        path = self.write(lines, **kw)
        events, stats = afx.tokenise(path, VENDOR)
        diag = afx.assign_dates(events)
        return events, stats, diag

    def sessions_from(self, lines):
        events, _stats, _diag = self.parse(lines)
        return afx.build_sessions(events)


# ---------------------------------------------------------------- line parsing

class TestLineParsing(Base):

    def test_recognises_each_line_type(self):
        _ev, stats, _d = self.parse([
            ts("10:00:00", 2026, 3, 2),
            out("10:01:00", "Flex", "alice", "ws-a"),
            inn("10:02:00", "Flex", "alice", "ws-a"),
            line("10:03:00", 'UNSUPPORTED: "Wave" (PORT_AT_HOST_PLUS   ) '
                             'alice@ws-a  (No such feature exists. (-5,346))'),
            line("10:04:00", 'Checkin failed feature "Flex": alice ws-a ws-a'),
            line("10:05:00", "EXITING DUE TO SIGNAL 27 Exit reason 4"),
            line("10:06:00", "SLOG: Summary LOG statistics is enabled."),
        ])
        self.assertEqual(stats["checkout_lines"], 1)
        self.assertEqual(stats["checkin_lines"], 1)
        self.assertEqual(stats["denial_lines"], 1)
        self.assertEqual(stats["checkin_failed_lines"], 1)
        self.assertEqual(stats["daemon_reset_lines"], 1)
        self.assertEqual(stats["timestamp_lines"], 1)
        self.assertEqual(stats["ignored_lines"], 1)      # the SLOG line

    def test_space_padded_hour(self):
        """Real logs write single-digit hours as ' 4:27:57', not '04:27:57'."""
        events, _s, _d = self.parse([
            ts(" 4:00:00", 2026, 3, 2),
            out(" 4:27:57", "Flex", "alice", "ws-a"),
        ])
        self.assertEqual(events[-1].secs, 4 * 3600 + 27 * 60 + 57)

    def test_licence_key_blob_before_identity(self):
        """OUT lines may carry a key blob in parentheses before user@host."""
        events, _s, _d = self.parse([
            ts("10:00:00", 2026, 3, 2),
            line("10:01:00", 'OUT: "Flex" (00F2 6F5A 8ADE F6BD ) bob@ws-b  '),
        ])
        ev = events[-1]
        self.assertEqual((ev.kind, ev.feature, ev.user, ev.host),
                         ("OUT", "Flex", "bob", "ws-b"))

    def test_identities_are_casefolded(self):
        """One person spelled 'Ian' and 'ian' must not count as two hosts/users."""
        events, _s, _d = self.parse([
            ts("10:00:00", 2026, 3, 2),
            out("10:01:00", "Flex", "Ian", "Dell95"),
            out("10:02:00", "Flex", "ian", "dell95"),
        ])
        self.assertEqual({e.user for e in events if e.kind == "OUT"}, {"ian"})
        self.assertEqual({e.host for e in events if e.kind == "OUT"}, {"dell95"})

    def test_casefolding_can_be_disabled(self):
        afx.CASEFOLD_IDENTITIES = False
        events, _s, _d = self.parse([
            ts("10:00:00", 2026, 3, 2),
            out("10:01:00", "Flex", "Ian", "Dell95"),
            out("10:02:00", "Flex", "ian", "dell95"),
        ])
        self.assertEqual({e.user for e in events if e.kind == "OUT"}, {"Ian", "ian"})

    def test_other_vendor_tags_are_ignored(self):
        path = self.write([
            ts("10:00:00", 2026, 3, 2),
            "10:01:00 (someoneelse) OUT: \"Flex\" alice@ws-a  ",
        ])
        events, stats = afx.tokenise(path, VENDOR)
        self.assertEqual(stats["checkout_lines"], 0)
        self.assertEqual(stats["unparsed_lines"], 1)


# ---------------------------------------------------------------------- dating

class TestDating(Base):

    def test_events_take_the_date_of_the_preceding_anchor(self):
        events, _s, _d = self.parse([
            ts("10:00:00", 2026, 3, 2),
            out("11:00:00", "Flex", "alice", "ws-a"),
        ])
        self.assertEqual(events[-1].day, datetime(2026, 3, 2).date())

    def test_midnight_crossing_advances_the_day(self):
        events, _s, diag = self.parse([
            ts("23:00:00", 2026, 3, 2),
            out("23:59:00", "Flex", "alice", "ws-a"),
            inn("00:01:00", "Flex", "alice", "ws-a"),
        ])
        self.assertEqual(events[1].day, datetime(2026, 3, 2).date())
        self.assertEqual(events[2].day, datetime(2026, 3, 3).date())
        self.assertEqual(diag.get("rollovers_forward"), 1)

    def test_small_backwards_step_is_jitter_not_midnight(self):
        """Regression: the daemon writes from several threads, so a log can hold a
        16:22:25 line after a 16:26:24 one. Reading that as midnight invented
        extra days and produced sessions ending before they started."""
        events, _s, diag = self.parse([
            ts("16:00:00", 2026, 3, 2),
            out("16:26:24", "Flex", "alice", "ws-a"),
            out("16:22:25", "Flex", "bob", "ws-b"),
            inn("16:30:00", "Flex", "alice", "ws-a"),
        ])
        self.assertEqual(diag.get("rollovers_forward", 0), 0)
        self.assertEqual(diag.get("out_of_order_lines"), 1)
        for e in events[1:]:
            self.assertEqual(e.day, datetime(2026, 3, 2).date())

    def test_jitter_does_not_break_the_following_line(self):
        """After holding the high-water mark, the next in-order line must not be
        read as a huge jump forwards."""
        _ev, _s, diag = self.parse([
            ts("16:00:00", 2026, 3, 2),
            out("16:26:24", "Flex", "alice", "ws-a"),
            out("16:22:25", "Flex", "bob", "ws-b"),
            out("16:27:00", "Flex", "carol", "ws-c"),
        ])
        self.assertEqual(diag.get("rollovers_forward", 0), 0)

    def test_events_before_the_first_anchor_are_backfilled(self):
        events, _s, _d = self.parse([
            out("22:00:00", "Flex", "alice", "ws-a"),
            ts("23:00:00", 2026, 3, 2),
        ])
        self.assertEqual(events[0].day, datetime(2026, 3, 2).date())

    def test_backfill_steps_back_over_midnight(self):
        events, _s, diag = self.parse([
            out("23:30:00", "Flex", "alice", "ws-a"),
            ts("01:00:00", 2026, 3, 3),
        ])
        self.assertEqual(events[0].day, datetime(2026, 3, 2).date())
        self.assertEqual(diag.get("rollovers_backward"), 1)

    def test_anchor_wins_when_a_gap_hid_a_rollover(self):
        """A daemon idle for days leaves no events to infer midnight from, so the
        TIMESTAMP line has to be trusted over the walked date."""
        events, _s, diag = self.parse([
            ts("10:00:00", 2026, 3, 2),
            out("11:00:00", "Flex", "alice", "ws-a"),
            ts("12:00:00", 2026, 3, 6),
            out("13:00:00", "Flex", "bob", "ws-b"),
        ])
        self.assertEqual(diag.get("anchor_corrections"), 1)
        self.assertEqual(diag.get("anchor_drift_days"), 4)
        self.assertEqual(events[-1].day, datetime(2026, 3, 6).date())

    def test_no_timestamp_lines_is_a_clean_error(self):
        path = self.write([out("10:00:00", "Flex", "alice", "ws-a")])
        events, _stats = afx.tokenise(path, VENDOR)
        with self.assertRaises(SystemExit) as cm:
            afx.assign_dates(events)
        self.assertIn("TIMESTAMP", str(cm.exception))


# -------------------------------------------------------------------- sessions

class TestSessions(Base):

    def test_pairs_checkout_with_checkin(self):
        sessions, _diag, _resets = self.sessions_from([
            ts("09:00:00", 2026, 3, 2),
            out("09:30:00", "Flex", "alice", "ws-a"),
            inn("10:30:00", "Flex", "alice", "ws-a"),
        ])
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].seconds, 3600)
        self.assertEqual(sessions[0].flag, "closed")

    def test_pairs_oldest_outstanding_first(self):
        sessions, _d, _r = self.sessions_from([
            ts("09:00:00", 2026, 3, 2),
            out("09:00:10", "Flex", "alice", "ws-a"),
            out("09:00:20", "Flex", "alice", "ws-a"),
            inn("09:10:00", "Flex", "alice", "ws-a"),
            inn("09:20:00", "Flex", "alice", "ws-a"),
        ])
        self.assertEqual(len(sessions), 2)
        # FIFO: the first checkin closes the first checkout.
        self.assertEqual(sorted(s.seconds for s in sessions), [590, 1180])

    def test_restart_closes_open_handles_at_the_restart(self):
        """A restart genuinely releases every handle. Leaving them open would
        manufacture multi-week sessions and wildly inflate the peaks."""
        sessions, diag, resets = self.sessions_from([
            ts("09:00:00", 2026, 3, 2),
            out("09:00:00", "Flex", "alice", "ws-a"),
            line("11:00:00", "EXITING DUE TO SIGNAL 28 Exit reason 5"),
        ])
        self.assertEqual(diag.get("truncated_at_reset"), 1)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].flag, "truncated")
        self.assertEqual(sessions[0].seconds, 2 * 3600)
        self.assertEqual(len(resets), 1)

    def test_handles_open_at_log_end_are_flagged(self):
        sessions, diag, _r = self.sessions_from([
            ts("09:00:00", 2026, 3, 2),
            out("09:00:00", "Flex", "alice", "ws-a"),
            out("10:00:00", "Flex", "bob", "ws-b"),
            inn("10:30:00", "Flex", "bob", "ws-b"),
        ])
        self.assertEqual(diag.get("open_at_log_end"), 1)
        self.assertEqual([s.flag for s in sessions if s.user == "alice"], ["open"])

    def test_checkin_without_checkout_is_counted_not_invented(self):
        """A log that begins mid-session has checkins with nothing to close."""
        sessions, diag, _r = self.sessions_from([
            ts("09:00:00", 2026, 3, 2),
            inn("09:30:00", "Flex", "alice", "ws-a"),
        ])
        self.assertEqual(sessions, [])
        self.assertEqual(diag.get("orphan_checkins"), 1)

    def test_negative_durations_are_clamped_and_counted(self):
        """The canary on the dating logic: end < start cannot happen if dates are
        right, so it must be reported rather than silently produce a negative."""
        o = afx.Ev("OUT", 10 * 3600, "Flex", "alice", "ws-a")
        i = afx.Ev("IN", 9 * 3600, "Flex", "alice", "ws-a")
        o.day = datetime(2026, 3, 3).date()
        i.day = datetime(2026, 3, 2).date()          # deliberately impossible
        sessions, diag, _r = afx.build_sessions([o, i])
        self.assertEqual(diag.get("negative_duration_clamped"), 1)
        self.assertEqual(sessions[0].seconds, 0)


# ----------------------------------------------------------------- concurrency

class TestConcurrency(Base):

    def test_merge_intervals_unions_overlaps(self):
        merged = afx.merge_intervals([(dt(9), dt(11)), (dt(10), dt(12)), (dt(14), dt(15))])
        self.assertEqual(merged, [(dt(9), dt(12)), (dt(14), dt(15))])

    def test_one_host_holding_many_handles_is_one_licence(self):
        """The core of DUP_GROUP=HD. A parallel batch run holds hundreds of
        handles; they cost a single licence between them."""
        sessions = [session(host="ws-batch", start=dt(9, i), end=dt(11)) for i in range(50)]
        sets = afx.interval_sets(sessions)
        _t, peak, _at = afx.sweep(sets["Flex"]["hosts"], (dt(0), dt(24)), False)
        self.assertEqual(peak, 1)

    def test_distinct_hosts_add_up(self):
        sessions = [session(host="ws-a", start=dt(9), end=dt(12)),
                    session(host="ws-b", start=dt(10), end=dt(12)),
                    session(host="ws-c", start=dt(11), end=dt(12))]
        sets = afx.interval_sets(sessions)
        _t, peak, _at = afx.sweep(sets["Flex"]["hosts"], (dt(0), dt(24)), False)
        self.assertEqual(peak, 3)

    def test_same_second_handover_does_not_invent_a_peak(self):
        """The daemon logs to one second, so a checkin and checkout in the same
        second have no recorded order. Counting the acquire first would report a
        peak of 2 where only one licence was ever held."""
        sessions = [session(host="ws-a", start=dt(9), end=dt(10)),
                    session(host="ws-b", start=dt(10), end=dt(11))]
        sets = afx.interval_sets(sessions)
        _t, peak, _at = afx.sweep(sets["Flex"]["hosts"], (dt(0), dt(24)), False)
        self.assertEqual(peak, 1)

    def test_any_feature_aggregate_is_present(self):
        sessions = [session(feature="Flex", host="ws-a"),
                    session(feature="Other", host="ws-b")]
        sets = afx.interval_sets(sessions)
        self.assertIn(afx.ANY_FEATURE, sets)
        self.assertEqual(set(sets), {"Flex", "Other", afx.ANY_FEATURE})

    def test_worst_duplicate_holder_names_the_host(self):
        sessions = [session(host="ws-batch", start=dt(9), end=dt(10)) for _ in range(7)]
        sessions.append(session(host="ws-quiet", start=dt(9), end=dt(10)))
        dup = afx.worst_duplicate_holder(sessions, "Flex", (dt(0), dt(24)))
        self.assertEqual(dup["peak_handles"], 7)
        self.assertEqual(dup["host"], "ws-batch")

    def test_empty_interval_list_is_not_an_error(self):
        time_at, peak, at = afx.sweep([], (dt(0), dt(24)), False)
        self.assertEqual((sum(time_at.values()), peak, at), (0, 0, None))


# ---------------------------------------------------------------------- stats

class TestStats(Base):

    def test_time_at_level_accounting(self):
        # one host 09:00-11:00, a second 10:00-11:00
        ivs = [(dt(9), dt(11)), (dt(10), dt(11))]
        time_at, peak, _at = afx.sweep(ivs, (dt(9), dt(11)), False)
        self.assertEqual(peak, 2)
        self.assertEqual(time_at[1], 3600)      # 09:00-10:00 at one
        self.assertEqual(time_at[2], 3600)      # 10:00-11:00 at two

    def test_quantiles_are_time_weighted_not_per_event(self):
        """A brief spike must barely move p95; that is the behaviour sizing needs."""
        time_at = Counter({1: 9900.0, 9: 100.0})
        self.assertEqual(afx.quantile_from_time_at(time_at, 0.50), 1)
        self.assertEqual(afx.quantile_from_time_at(time_at, 0.95), 1)
        self.assertEqual(afx.quantile_from_time_at(time_at, 0.999), 9)

    def test_exceedance_starts_at_one_hundred_percent_and_decreases(self):
        curve = afx.exceedance(Counter({0: 50.0, 1: 30.0, 2: 20.0}))
        self.assertEqual(curve[0]["pct"], 100.0)
        self.assertAlmostEqual(curve[1]["pct"], 50.0)
        self.assertAlmostEqual(curve[2]["pct"], 20.0)
        pcts = [c["pct"] for c in curve]
        self.assertEqual(pcts, sorted(pcts, reverse=True))

    def test_business_seconds_counts_only_working_hours(self):
        # Monday 09:00-10:00 is inside 08:00-18:00
        self.assertEqual(afx.business_seconds(dt(9), dt(10)), 3600)
        # 07:00-09:00 contributes only the hour after 08:00
        self.assertEqual(afx.business_seconds(dt(7), dt(9)), 3600)

    def test_business_seconds_excludes_the_weekend(self):
        sat = datetime(2026, 3, 7, 9, 0)         # Saturday
        self.assertEqual(afx.business_seconds(sat, sat + timedelta(hours=2)), 0)

    def test_business_seconds_spans_multiple_days(self):
        # Mon 17:00 to Tue 09:00: one hour Monday, one hour Tuesday
        self.assertEqual(afx.business_seconds(dt(17), dt(9, days=1)), 2 * 3600)

    def test_business_label_reflects_configuration(self):
        afx.BUSINESS_START_HOUR, afx.BUSINESS_END_HOUR = 9, 17
        afx.BUSINESS_DAYS = {0, 1, 2, 3, 4}
        self.assertEqual(afx.business_label(), "Mon-Fri 09:00-17:00")
        afx.BUSINESS_DAYS = {0, 2}
        self.assertEqual(afx.business_label(), "Mon,Wed 09:00-17:00")

    def test_licence_hours_and_idle_share(self):
        stats = afx.stats_for([(dt(9), dt(10))], (dt(9), dt(11)), False)
        self.assertEqual(stats["peak"], 1)
        self.assertAlmostEqual(stats["licence_hours"], 1.0)
        self.assertAlmostEqual(stats["pct_time_idle"], 50.0)

    def test_duration_histogram_bins(self):
        sessions = [
            session(start=dt(9), end=dt(9, 0, seconds=30)),      # < 1 min
            session(start=dt(9), end=dt(9, 3)),                  # 1-5 min
            session(start=dt(9), end=dt(10, 30)),                # 90 min -> 1-2 h
            session(start=dt(9), end=dt(11)),                    # exactly 120 min
        ]
        hist = {b["label"]: b["count"] for b in afx.duration_histogram(sessions)}
        self.assertEqual(hist["< 1 min"], 1)
        self.assertEqual(hist["1-5 min"], 1)
        self.assertEqual(hist["1-2 h"], 1)
        # Bins are half-open: 120 minutes is the start of the next one, not the
        # end of the previous.
        self.assertEqual(hist["2-4 h"], 1)
        self.assertEqual(hist["> 24 h"], 0)

    def test_per_user_reports_handle_hours_and_duplicate_peak(self):
        sessions = [session(user="alice", host="ws-a", start=dt(9), end=dt(10))
                    for _ in range(3)]
        rows = {r["user"]: r for r in afx.per_user(sessions)}
        self.assertEqual(rows["alice"]["sessions"], 3)
        self.assertAlmostEqual(rows["alice"]["handle_hours"], 3.0)
        self.assertEqual(rows["alice"]["peak_concurrent_handles"], 3)


# ------------------------------------------------------------------ arg parsing

class TestArgParsing(Base):

    def test_business_hours(self):
        self.assertEqual(afx.parse_hours("8-18"), (8, 18))
        self.assertEqual(afx.parse_hours(" 9 - 17 "), (9, 17))

    def test_business_hours_rejects_nonsense(self):
        import argparse
        for bad in ("18-8", "8", "8-25", "", "eight-six"):
            with self.assertRaises(argparse.ArgumentTypeError):
                afx.parse_hours(bad)

    def test_business_days(self):
        self.assertEqual(afx.parse_days("mon,tue,wed,thu,fri"), [0, 1, 2, 3, 4])
        self.assertEqual(afx.parse_days("Sun,mon"), [0, 6])

    def test_business_days_rejects_nonsense(self):
        import argparse
        for bad in ("", "funday", "mon,funday"):
            with self.assertRaises(argparse.ArgumentTypeError):
                afx.parse_days(bad)


# ------------------------------------------------------------------ end to end

def run(*args):
    """Run a tool as a subprocess, returning (returncode, stdout+stderr)."""
    proc = subprocess.run([sys.executable] + list(args),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


class TestEndToEnd(Base):

    def busy_log(self, days=3):
        lines = [line("08:00:00", f"Server started on TESTSRV for:\tFlex\t")]
        for d in range(days):
            day = BASE + timedelta(days=d)
            lines.append(ts("07:00:00", day.year, day.month, day.day))
            for h in (9, 11, 14):
                lines.append(out(f"{h:02d}:00:00", "Flex", "alice", "ws-a"))
                lines.append(inn(f"{h:02d}:30:00", "Flex", "alice", "ws-a"))
                lines.append(out(f"{h:02d}:05:00", "Flex", "bob", "ws-b"))
                lines.append(inn(f"{h:02d}:50:00", "Flex", "bob", "ws-b"))
        return lines

    def test_full_pipeline_produces_a_report(self):
        log = self.write(self.busy_log())
        js = os.path.join(self.tmp, "data.json")
        html = os.path.join(self.tmp, "report.html")
        rc, txt = run(ANALYSER, log, "-o", js)
        self.assertEqual(rc, 0, txt)
        rc, txt = run(BUILDER, "-d", js, "-o", html)
        self.assertEqual(rc, 0, txt)

        with open(js, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["meta"]["charted_features"], ["Flex"])
        self.assertEqual(data["slices"]["all|all"]["features"]["Flex"]["hosts"]["peak"], 2)
        with open(html, encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn("Sizing curve", page)

    def test_quiet_server_with_no_recent_activity(self):
        """Regression: a log whose last 7 and 30 days hold no checkouts crashed
        with KeyError, because an empty window yields no interval sets at all."""
        lines = [ts("10:00:00", 2026, 3, 2),
                 out("10:30:00", "Flex", "alice", "ws-a"),
                 inn("10:45:00", "Flex", "alice", "ws-a")]
        # 45 further days of an idle server still writing its anchors.
        for k in range(45 * 4):
            a = datetime(2026, 3, 2, 10, 22, 57) + timedelta(hours=6 * k)
            lines.append(ts(f"{a.hour:2d}:{a.minute:02d}:{a.second:02d}",
                            a.year, a.month, a.day))
        log = self.write(lines)
        js = os.path.join(self.tmp, "data.json")
        rc, txt = run(ANALYSER, log, "-o", js)
        self.assertEqual(rc, 0, txt)

        with open(js, encoding="utf-8") as fh:
            data = json.load(fh)
        # The feature must still be charted even though it is under --min-sessions,
        # or every chart in the report renders with no series at all.
        self.assertEqual(data["meta"]["charted_features"], ["Flex"])
        self.assertEqual(data["heatmap"]["d7"]["hosts"], [])
        self.assertEqual(data["slices"]["d7|all"]["features"], {})
        self.assertIn("only 1 session", txt)

        html = os.path.join(self.tmp, "r.html")
        rc, txt = run(BUILDER, "-d", js, "-o", html)
        self.assertEqual(rc, 0, txt)

    def test_crlf_and_lf_logs_give_identical_results(self):
        """Client logs come from Windows licence servers and are CRLF."""
        lines = self.busy_log()
        results = []
        for i, nl in enumerate(("\n", "\r\n")):
            log = self.write(lines, newline=nl, name=f"v{i}.log")
            js = os.path.join(self.tmp, f"d{i}.json")
            rc, txt = run(ANALYSER, log, "-o", js)
            self.assertEqual(rc, 0, txt)
            with open(js, encoding="utf-8") as fh:
                data = json.load(fh)
            # These two legitimately differ between the runs.
            data["meta"].pop("generated")
            data["meta"].pop("log")
            results.append(data)
        self.assertEqual(results[0], results[1])

    def test_missing_timestamp_lines_explains_itself(self):
        log = self.write([out("10:00:00", "Flex", "alice", "ws-a"),
                          inn("10:30:00", "Flex", "alice", "ws-a")])
        rc, txt = run(ANALYSER, log, "-o", os.path.join(self.tmp, "x.json"))
        self.assertNotEqual(rc, 0)
        self.assertIn("TIMESTAMP", txt)
        self.assertNotIn("Traceback", txt)

    def test_wrong_vendor_explains_itself(self):
        log = self.write(self.busy_log())
        rc, txt = run(ANALYSER, log, "--vendor", "notorcina",
                      "-o", os.path.join(self.tmp, "x.json"))
        self.assertNotEqual(rc, 0)
        self.assertIn("vendor", txt.lower())
        self.assertNotIn("Traceback", txt)

    def test_log_of_only_checkins_explains_itself(self):
        """Nothing can be paired, so there is no duration to measure."""
        log = self.write([ts("10:00:00", 2026, 3, 2),
                          inn("10:30:00", "Flex", "alice", "ws-a")])
        rc, txt = run(ANALYSER, log, "-o", os.path.join(self.tmp, "x.json"))
        self.assertNotEqual(rc, 0)
        self.assertNotIn("Traceback", txt)

    def test_business_hours_options_reach_the_output(self):
        log = self.write(self.busy_log())
        js = os.path.join(self.tmp, "data.json")
        rc, txt = run(ANALYSER, log, "-o", js,
                      "--business-hours", "9-17", "--business-days", "mon,tue")
        self.assertEqual(rc, 0, txt)
        with open(js, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["meta"]["business_hours"],
                             "Mon-Tue 09:00-17:00")

    def test_report_escapes_markup_from_the_log(self):
        """Feature names, usernames and hostnames are untrusted text. A feature
        containing '</script>' must not be able to close the payload element."""
        lines = [ts("10:00:00", 2026, 3, 2),
                 out("10:01:00", "a</script><b>", "alice", "ws-a"),
                 inn("10:02:00", "a</script><b>", "alice", "ws-a")]
        log = self.write(lines)
        js = os.path.join(self.tmp, "d.json")
        html = os.path.join(self.tmp, "r.html")
        self.assertEqual(run(ANALYSER, log, "-o", js)[0], 0)
        self.assertEqual(run(BUILDER, "-d", js, "-o", html)[0], 0)
        with open(html, encoding="utf-8") as fh:
            page = fh.read()
        self.assertNotIn("a</script>", page)
        self.assertIn("\\u003c/script", page)


class TestSampleGenerator(Base):
    """The committed sample must keep exercising the paths it exists to cover."""

    def test_sample_log_covers_the_awkward_paths(self):
        gen = os.path.join(ROOT, "make_sample_log.py")
        log = os.path.join(self.tmp, "sample.log")
        rc, txt = run(gen, "-o", log, "--days", "21", "--seed", "42")
        self.assertEqual(rc, 0, txt)
        js = os.path.join(self.tmp, "sample.json")
        rc, txt = run(ANALYSER, log, "-o", js)
        self.assertEqual(rc, 0, txt)
        with open(js, encoding="utf-8") as fh:
            meta = json.load(fh)["meta"]

        self.assertGreater(meta["date_diagnostics"].get("rollovers_forward", 0), 0)
        self.assertGreater(meta["date_diagnostics"].get("out_of_order_lines", 0), 0)
        self.assertGreater(meta["session_diagnostics"].get("truncated_at_reset", 0), 0)
        self.assertGreater(meta["session_diagnostics"].get("open_at_log_end", 0), 0)
        self.assertGreater(meta["session_diagnostics"].get("orphan_checkins", 0), 0)
        self.assertTrue(meta["shared_hosts"], "a shared host must be exercised")
        # Dates must come out clean: a negative duration means the walk is wrong.
        self.assertEqual(meta["session_diagnostics"].get("negative_duration_clamped", 0), 0)

    def test_sample_uses_no_real_identities(self):
        """The committed sample is public. Every name in it must be invented."""
        path = os.path.join(ROOT, "sample", "sample_vendor.log")
        if not os.path.exists(path):
            self.skipTest("sample log not present")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        allowed = {"alice", "bob", "carol", "dan", "erin", "frank", "grace", "heidi"}
        import re
        users = set(re.findall(r'"[^"]*" (?:\([^)]*\) )?([^@\s]+)@', text))
        self.assertTrue(users <= allowed, f"unexpected identities: {users - allowed}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
