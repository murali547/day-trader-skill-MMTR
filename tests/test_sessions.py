"""Futures sessions (--session): Globex, Asia, London, COMEX or HH:MM-HH:MM, including the
night sessions whose bars run across midnight. The charts here are synthetic: real Yahoo
futures bars for a night session are not in the fixtures."""

import datetime
import io
import json
import random
import unittest
from contextlib import redirect_stdout

from _load import load

entry = load("trader-entry", "entry")
ex = load("trader-exit", "exit")
tm = load("trader-timemachine", "timemachine")
size = load("trader-budget", "size")
check = load("trader-risk", "check")
alerts = load("trader-alerts", "alerts")

ASIA = {"name": "asia", "open": "18:00", "close": "03:00", "minutes": 540}


def ny(text):
    return entry.eastern_to_utc(datetime.datetime.strptime(text, "%Y-%m-%d %H:%M"))


def chart(symbol, bars, data_time=None):
    """Yahoo v8 chart JSON from [(New York 'YYYY-MM-DD HH:MM', o, h, l, c)]."""
    ts = [int((ny(t) - entry.EPOCH).total_seconds()) for t, *_ in bars]
    q = {"open": [b[1] for b in bars], "high": [b[2] for b in bars], "low": [b[3] for b in bars],
         "close": [b[4] for b in bars], "volume": [1000] * len(bars)}
    meta = {"symbol": symbol, "dataGranularity": "5m", "regularMarketTime": data_time or (ts[-1] + 300 if ts else 0)}
    return json.dumps({"chart": {"result": [{"meta": meta, "timestamp": ts, "indicators": {"quote": [q]}}]}})


def walk(start, n, first, step_min=5):
    """n bars every 5 minutes from New York time `start`, flat at `first` unless overridden later."""
    t0 = datetime.datetime.strptime(start, "%Y-%m-%d %H:%M")
    return [((t0 + datetime.timedelta(minutes=step_min * i)).strftime("%Y-%m-%d %H:%M"), first, first + 1,
             first - 1, first) for i in range(n)]


# Sunday 2026-10-25 18:00 to Monday 03:00: the asia session of trade date Monday 2026-10-26.
# Range 18:00-18:15 is 99..101 (bars at 100); 18:15 closes at 100.5, 18:20 closes at 102 (the breakout).
SUNDAY = walk("2026-10-25 18:00", 108, 100.0)
SUNDAY[3] = (SUNDAY[3][0], 100, 101, 99.5, 100.5)
SUNDAY[4] = (SUNDAY[4][0], 100.5, 102.5, 100.4, 102.0)


class Tables(unittest.TestCase):
    def test_every_tool_carries_the_same_sessions(self):
        for mod in (ex, tm):
            self.assertEqual(mod.SESSIONS, entry.SESSIONS)
        self.assertEqual(entry.SESSIONS["asia"], ("18:00", "03:00"))


class Entry(unittest.TestCase):
    def evaluate(self, when, text=None, sess="asia"):
        ch = entry.parse_chart(text or chart("NQ=F", SUNDAY), "NQ=F")
        return entry.evaluate(ch, ny(when), sess=entry.parse_session(sess))

    def test_night_session_breakout(self):
        r = self.evaluate("2026-10-25 18:30")
        self.assertEqual(r["status"], "triggered", r["reason"])
        self.assertEqual(r["date"], "2026-10-26")                     # trade date: the day it closes
        self.assertEqual((r["opening_range"]["high"], r["opening_range"]["low"]), (101.0, 99.0))
        self.assertEqual((r["trigger"]["time"], r["trigger"]["price"], r["trigger"]["bar_index"]), ("6:25 PM", 102.0, 4))
        self.assertEqual((r["no_entries_before"], r["no_entries_after"]), ("18:15", "02:30"))
        doc = entry.entry_doc(r, ny("2026-10-25 18:30"))
        self.assertEqual(doc["session"]["name"], "asia")

    def test_bars_after_midnight_belong_to_the_same_session(self):
        bars = walk("2026-10-25 18:00", 108, 100.0)          # nothing breaks out before midnight
        bars[80] = (bars[80][0], 100, 103, 100, 102.5)        # 18:00 + 400 min = 00:40 Monday
        r = self.evaluate("2026-10-26 01:00", chart("NQ=F", bars))
        self.assertEqual(r["status"], "triggered", r["reason"])
        self.assertEqual((r["trigger"]["bar_start"], r["trigger"]["time"]), ("00:40", "12:45 AM"))
        r = self.evaluate("2026-10-26 02:40", chart("NQ=F", walk("2026-10-25 18:00", 108, 100.0)))
        self.assertEqual(r["status"], "expired")
        self.assertIn("02:30", r["reason"])

    def test_pre_open_closed_and_next_open(self):
        r = self.evaluate("2026-10-25 17:30")
        self.assertEqual((r["status"], r["phase"], r["date"]), ("waiting", "pre-open", "2026-10-26"))
        self.assertIn("the asia session opens at 18:00", r["reason"])
        r = self.evaluate("2026-10-24 12:00")                 # Saturday
        self.assertEqual((r["status"], r["phase"]), ("no-data", "closed"))
        self.assertEqual(r["next_open"], "2026-10-25 18:00")

    def test_globex_maintenance_hour_waits_for_the_reopen(self):
        found = entry.session_at(datetime.datetime(2026, 10, 27, 17, 30), entry.parse_session("globex"))
        self.assertEqual((found[0], found[1]), (datetime.date(2026, 10, 28), datetime.datetime(2026, 10, 27, 18, 0)))

    def test_london_and_custom_sessions(self):
        self.assertEqual(entry.parse_session("london")["minutes"], 390)
        self.assertEqual(entry.parse_session("20:00-03:00"),
                         {"name": "custom", "open": "20:00", "close": "03:00", "minutes": 420})
        self.assertIsNone(entry.parse_session("ny"))
        with self.assertRaises(entry.EntryError):
            entry.parse_session("tokyo")

    def test_session_is_for_futures_only(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()):
            import contextlib
            with contextlib.redirect_stderr(err):
                rc = entry.main(["check", "TSLA", "--session", "asia", "--offline", "/nonexistent"])
        self.assertEqual(rc, 1)
        self.assertIn("is for futures", err.getvalue())


class Exit(unittest.TestCase):
    def plan(self, *extra):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ex.main(["plan", "--symbol", "NQ=F", "--entry", "29130.5", "--or-high", "29127", "--or-low", "28987",
                          "--stop", "or-mid", "--target", "1R", "--session", "asia", "--time", "6:25 PM",
                          "--now", "2026-10-25 18:25", "--config", "-", "--json"] + list(extra))
        return rc, out.getvalue()

    def test_night_plan_defaults(self):
        rc, out = self.plan()
        self.assertEqual(rc, 0)
        p = json.loads(out)
        self.assertEqual((p["date"], p["flat_by"], p["session"]["name"]), ("2026-10-26", "02:55", "asia"))
        self.assertEqual(p["warnings"], ["Flat-by 02:55: 5 minutes before the asia session (18:00 to 03:00) close."])
        text = "\n".join(ex.describe(p))
        self.assertIn("Session       asia, 18:00 to 03:00 New York (trade date 2026-10-26)", text)

    def test_flat_by_and_entry_must_sit_inside_the_session(self):
        self.assertEqual(json.loads(self.plan("--flat-by", "01:30")[1])["flat_by"], "01:30")
        with self.assertRaises(ex.PlanError) as e:
            ex.session_flat_by(datetime.date(2026, 10, 26), ex.parse_session("asia"), "04:00", None)
        self.assertIn("outside the asia session", str(e.exception))
        with self.assertRaises(ex.PlanError) as e:
            ex.session_flat_by(datetime.date(2026, 10, 26), ex.parse_session("asia"), "01:00",
                               datetime.time(1, 30))
        self.assertIn("at or after your flat-by", str(e.exception))
        # 23:00 is before 02:55 inside the night session, though not on the clock
        self.assertEqual(ex.session_flat_by(datetime.date(2026, 10, 26), ex.parse_session("asia"), None,
                                            datetime.time(23, 0))[0], "02:55")


def asia_history(days=30, seed=3, gaps=0):
    """`days` weekday asia sessions before trade date 2026-10-26 plus that night's first 5 bars."""
    rng = random.Random(seed)
    bars, d = [], datetime.date(2026, 10, 26)
    trade_dates = []
    while len(trade_dates) < days:
        d -= datetime.timedelta(days=1)
        if d.weekday() < 5:
            trade_dates.append(d)
    for td in sorted(trade_dates):
        start = datetime.datetime.combine(td - datetime.timedelta(days=1), datetime.time(18, 0))
        p = 100.0
        for i in range(108):
            if gaps and i in (50, 51) and td == trade_dates[0]:
                continue                                      # two quiet bars with no trades
            c = p * (1 + rng.uniform(-0.002, 0.002))
            bars.append(((start + datetime.timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M"),
                         p, max(p, c) * 1.001, min(p, c) * 0.999, c))
            p = c
    return bars + SUNDAY[:5]


class TimeMachine(unittest.TestCase):
    PLAN = {"symbol": "NQ=F", "side": "buy", "date": "2026-10-26", "setup": "orb", "entry": 102.0,
            "entry_time": "6:25 PM", "stop": 100.0, "target": 104.0, "stop_rule": "or_mid",
            "opening_range": {"high": 101.0, "low": 99.0, "minutes": 15}, "session": ASIA}

    def test_night_replay(self):
        p = tm.normalize_plan(self.PLAN)
        self.assertEqual((p["entry_bar"], p["entry_time"], p["session"]["name"]), (4, "6:25 PM", "asia"))
        doc = tm.time_machine(chart("NQ=F", asia_history()), p, ny("2026-10-25 18:40"), replays=500)
        self.assertEqual(doc["mc"]["pool_sessions"], 30)
        self.assertEqual(doc["mc"]["bars_left"], 103)                # 6:25 PM to 3:00 AM
        self.assertEqual(doc["mc"]["until"], "03:00")
        self.assertEqual(doc["mc"]["band_times"][-1], "03:00")
        self.assertEqual(doc["history"]["sessions"], 30)
        text = "\n".join(tm.report(doc))
        self.assertIn("6:25 PM to 3:00 AM (103 five-minute bars)", text)
        self.assertIn("Session: asia, 18:00 to 03:00 New York, trade date 2026-10-26", text)

    def test_until_after_midnight_and_quiet_bars(self):
        p = tm.normalize_plan(self.PLAN)
        doc = tm.time_machine(chart("NQ=F", asia_history(gaps=2)), p, ny("2026-10-25 18:40"), replays=200,
                              until="02:00")
        self.assertEqual((doc["mc"]["until"], doc["mc"]["bars_left"]), ("02:00", 91))
        self.assertEqual(doc["mc"]["pool_sessions"], 30)
        self.assertEqual(doc["mc"]["filled_bars"], 2)
        self.assertIn("2 quiet bars with no trades were filled flat", "\n".join(tm.report(doc)))

    def test_stock_plans_are_unchanged(self):
        self.assertIs(tm.session_of(None), tm.RTH)
        self.assertEqual(tm.parse_clock("3:55", "t"), 15 * 60 + 55)    # a bare 3:55 still means p.m.
        self.assertEqual(tm.parse_clock("03:55", "t"), 3 * 60 + 55)


class Budget(unittest.TestCase):
    def test_trade_date_of_a_night_plan(self):
        self.assertEqual(size.session_trade_date(ASIA, datetime.datetime(2026, 10, 25, 19, 0)),
                         datetime.date(2026, 10, 26))
        self.assertEqual(size.session_trade_date(ASIA, datetime.datetime(2026, 10, 26, 2, 0)),
                         datetime.date(2026, 10, 26))

    def test_order_carries_the_session(self):
        plan = {"symbol": "MNQ=F", "side": "buy", "date": "2026-10-26", "setup": "orb", "entry": 29130.5,
                "stop": 29057.0, "target": 29204.0, "flat_by": "02:55", "session": ASIA}
        s = size.size_trade(plan, {"budget": 5000, "max_loss_per_trade_usd": 300}, today="2026-10-26")
        o = size.bracket_order(s)
        self.assertEqual((o["plan"]["session"], o["plan"]["date"]), (ASIA, "2026-10-26"))


class Risk(unittest.TestCase):
    def test_session_window(self):
        sun = datetime.datetime(2026, 10, 25, 18, 10)
        self.assertIn("no new entries before 18:15", check.session_window_check(sun, ASIA)[0])
        self.assertEqual(check.session_window_check(datetime.datetime(2026, 10, 25, 23, 0), ASIA), [])
        self.assertEqual(check.session_window_check(datetime.datetime(2026, 10, 26, 1, 0), ASIA), [])
        self.assertIn("no new entries after 02:30", check.session_window_check(datetime.datetime(2026, 10, 26, 2, 45),
                                                                               ASIA)[0])
        self.assertIn("is closed", check.session_window_check(datetime.datetime(2026, 10, 24, 12, 0), ASIA)[0])
        # the night before Thanksgiving is Thanksgiving's session: closed
        self.assertIn("Thanksgiving", check.session_window_check(datetime.datetime(2026, 11, 25, 20, 0), ASIA)[0])

    def test_futures_order_uses_its_session_not_the_nyse_window(self):
        plan = {"symbol": "MNQ=F", "side": "buy", "date": "2026-10-26", "setup": "orb", "entry": 29130.5,
                "stop": 29057.0, "target": 29204.0, "flat_by": "02:55", "session": ASIA}
        o = size.bracket_order(size.size_trade(plan, {"budget": 5000, "max_loss_per_trade_usd": 300},
                                               today="2026-10-26"))
        cfg = {"budget": 5000, "allow_futures": True, "max_loss_per_trade_usd": 300, "max_daily_loss_usd": 300}
        r = check.check([o], cfg, now=ny("2026-10-25 19:00"))["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        r = check.check([o], cfg, now=ny("2026-10-26 10:00"))["results"][0]   # the NYSE is open, the session is not
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any("asia session" in x for x in r["reasons"]), r["reasons"])


class Alerts(unittest.TestCase):
    def test_night_plan_alerts_watch_both_calendar_days(self):
        plan = {"symbol": "NQ=F", "date": "2026-10-26", "stop": 29057.0, "target": 29204.0, "flat_by": "02:55",
                "session": ASIA}
        stop, target, flat = alerts.rules_from_plan(plan)
        self.assertEqual(stop["dates"], ["2026-10-25", "2026-10-26"])
        self.assertTrue(alerts.active(stop, "2026-10-25") and alerts.active(target, "2026-10-26"))
        self.assertEqual((flat["date"], flat["at"], flat["before_close"]), ("2026-10-26", "02:55", False))
        london = alerts.rules_from_plan(dict(plan, session={"name": "london", "open": "03:00", "close": "09:30",
                                                            "minutes": 390}))
        self.assertEqual(london[0]["date"], "2026-10-26")


if __name__ == "__main__":
    unittest.main()
