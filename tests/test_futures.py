"""Futures (NQ=F, MNQ=F, GC=F, MGC=F) through exit, time machine, budget, risk and broker.

The numbers are Friday 2026-10-23's NQ opening-range breakout: range 28,987 to
29,127, the 9:45 bar closed at 29,130.50 (triggered at 9:50)."""

import gzip
import io
import json
import os
import unittest
from contextlib import redirect_stdout

from _load import load, ROOT, FIX

ex = load("trader-exit", "exit")
tm = load("trader-timemachine", "timemachine")
size = load("trader-budget", "size")
check = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")

with open(os.path.join(ROOT, "trader-budget", "config.example.json")) as _fh:
    CFG = json.load(_fh)

NQ_PLAN = {"created_by": "trader-exit", "symbol": "NQ=F", "side": "buy", "date": "2026-10-23", "setup": "orb",
           "entry": 29130.5, "entry_time": "9:50 AM", "entry_bar": 3, "interval": "5m",
           "opening_range": {"high": 29127.0, "low": 28987.0, "minutes": 15},
           "stop": 29057.0, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
           "target": 29204.0, "target_r": 1.0, "target_label": "1R (one times the risk)",
           "risk_per_share": 73.5, "reward_risk": 1.0, "flat_by": "15:55"}
NOW = check.parse_now("2026-10-23 09:50")
TODAY = "2026-10-23"


def plan(**kw):
    p = dict(NQ_PLAN)
    p.update(kw)
    return p


def cfg(**kw):
    c = dict(CFG)
    c.update(kw)
    return c


def sized(p=None, **kw):
    s = size.size_trade(p or plan(), cfg(**kw), today=TODAY)
    return s, size.bracket_order(s)


class Specs(unittest.TestCase):
    def test_every_tool_carries_the_same_contract_table(self):
        tables = [ex.FUTURES, tm.FUTURES, size.FUTURES, check.FUTURES]
        for t in tables[1:]:
            self.assertEqual(t, tables[0])
        self.assertEqual({k: v["point_value"] for k, v in ex.FUTURES.items()},
                         {"NQ=F": 20.0, "MNQ=F": 2.0, "GC=F": 100.0, "MGC=F": 10.0})
        self.assertEqual({k: v["tick"] for k, v in ex.FUTURES.items()},
                         {"NQ=F": 0.25, "MNQ=F": 0.25, "GC=F": 0.10, "MGC=F": 0.10})


class Exit(unittest.TestCase):
    def test_nq_or_mid_1r(self):
        c = ex.compute(29130.5, "or_mid", None, "r", ex.D(1), 29127, 28987, tick=0.25)
        self.assertEqual((str(c["stop"]), str(c["target"]), str(c["risk"])), ("29057.00", "29204.00", "73.50"))

    def test_stop_floors_and_target_rounds_to_the_tick(self):
        # gold: (4044.00 + 4035.70) / 2 = 4039.85 -> 4039.80 on the 0.10 tick
        c = ex.compute(4044.5, "or_mid", None, "r", ex.D(1), 4044.0, 4035.7, tick=0.10)
        self.assertEqual((str(c["stop"]), str(c["target"])), ("4039.80", "4049.20"))
        self.assertIn("rounded down", c["stop_detail"])
        # NQ: a 1.5R target off 0.25-tick prices lands on a quarter
        c = ex.compute(29130.5, "price", ex.D("29100.25"), "r", ex.D("1.5"), tick=0.25)
        self.assertEqual(str(c["target"]), "29176.00")   # 29130.5 + 45.375 = 29175.875 -> 29176.00

    def test_plan_carries_the_contract(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ex.main(["plan", "--symbol", "nq=f", "--entry", "29130.5", "--or-high", "29127", "--or-low", "28987",
                          "--stop", "or-mid", "--target", "1R", "--date", "2026-10-23", "--time", "9:50 AM",
                          "--now", "2026-10-23 09:50", "--config", "-", "--json"])
        self.assertEqual(rc, 0)
        p = json.loads(out.getvalue())
        self.assertEqual((p["symbol"], p["stop"], p["target"], p["risk_per_share"]), ("NQ=F", 29057.0, 29204.0, 73.5))
        self.assertEqual(p["contract"]["root"], "NQ")
        self.assertEqual(p["risk_per_contract_usd"], 1470.0)
        text = "\n".join(ex.describe(p))
        self.assertIn("73.5 points x $20.00 a point = $1,470.00 a contract", text)
        self.assertNotIn("Risk a share", text)

    def test_a_stock_plan_is_unchanged(self):
        c = ex.compute(364.18, "or_mid", None, "r", ex.D(1), 362.18, 354.47)
        self.assertEqual((str(c["stop"]), str(c["target"])), ("358.32", "370.04"))


class TimeMachine(unittest.TestCase):
    def test_accepts_a_futures_symbol_and_prices_the_contract(self):
        p = tm.normalize_plan(plan())
        self.assertEqual((p["symbol"], p["point_value"]), ("NQ=F", 20.0))
        self.assertIsNone(tm.normalize_plan(plan(symbol="TSLA"))["point_value"])

    def test_report_reads_in_points_and_dollars(self):
        # TSLA's real bars relabelled as NQ=F, the reel's prices: the replay math is the same
        with gzip.open(os.path.join(FIX, "trader-timemachine", "TSLA-60d5m.json.gz"), "rt", encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["chart"]["result"][0]["meta"]["symbol"] = "NQ=F"
        p = tm.normalize_plan(dict(plan(), date="2026-10-19", entry=364.18, stop=358.32, target=370.04,
                                   entry_time="10:15 AM", entry_bar=8, opening_range={"minutes": 15}))
        out = tm.time_machine(json.dumps(doc), p, tm.parse_now("2026-10-19 10:50"))
        line = tm.report(out)[0]
        self.assertIn("risk 5.86 points, $117.20 a contract", line)
        self.assertEqual(out["plan"]["point_value"], 20.0)


class Budget(unittest.TestCase):
    def test_full_size_nq_is_refused_at_a_50_dollar_max_loss(self):
        s, order = sized()
        self.assertIsNone(order)
        self.assertEqual((s["qty"], s["bound_by"], s["risk_per_contract_usd"]), (0, "max loss", 1470.0))
        self.assertIn("one NQ contract risks $1,470 (73.5 points x $20)", s["refusal"])
        self.assertIn("trade the micro, MNQ ($2 a point)", s["refusal"])

    def test_micro_sizes_in_contracts(self):
        s, order = sized(plan(symbol="MNQ=F"), max_loss_per_trade_usd=300, max_contracts=3)
        self.assertEqual((s["qty"], s["bound_by"], s["max_loss"]), (2, "max loss", 294.0))
        self.assertEqual({l["name"] for l in s["limits"]}, {"max loss", "max contracts"})
        self.assertIn("margin not checked", s["margin_note"])
        self.assertEqual(order["asset_class"], "future")
        self.assertEqual((order["symbol"], order["qty"], order["stop_loss_stop_price"], order["take_profit_limit_price"]),
                         ("MNQ=F", "2", "29057.00", "29204.00"))
        self.assertEqual(order["contract"]["root"], "MNQ")
        self.assertEqual(order["plan"]["point_value"], 2.0)
        text = size.plan_report(s, order)
        self.assertIn("Size: 2 MNQ contracts. Max loss at the stop: 2 x $147 = $294.", text)
        self.assertIn("Alpaca does not trade futures", text)

    def test_max_contracts_defaults_to_one(self):
        s, _ = sized(plan(symbol="MNQ=F"), max_loss_per_trade_usd=300)
        self.assertEqual((s["qty"], s["bound_by"]), (1, "max contracts"))

    def test_margin_from_the_config(self):
        s, _ = sized(plan(symbol="MNQ=F"), max_loss_per_trade_usd=1000, max_contracts=5,
                     futures_margin_usd={"MNQ": 2000})
        # $5,000 budget - $500 buffer = $4,500 / $2,000 = 2
        self.assertEqual((s["qty"], s["bound_by"], s["cost"]), (2, "margin", 4000.0))
        s, order = sized(plan(symbol="MNQ=F"), max_loss_per_trade_usd=1000, futures_margin_usd={"MNQ": 9000})
        self.assertIsNone(order)
        self.assertIn("needs $9,000 margin", s["refusal"])

    def test_gold(self):
        p = plan(symbol="MGC=F", entry=4044.5, stop=4039.8, target=4049.2, risk_per_share=4.7)
        s, order = sized(p, max_loss_per_trade_usd=100)
        self.assertEqual((s["qty"], s["risk_per_contract_usd"], s["max_loss"]), (1, 47.0, 47.0))
        s, order = sized(dict(p, symbol="GC=F"), max_loss_per_trade_usd=100)
        self.assertIsNone(order)
        self.assertIn("trade the micro, MGC ($10 a point)", s["refusal"])

    def test_unknown_future_and_blocked_root(self):
        with self.assertRaises(size.SizeError) as e:
            sized(plan(symbol="ES=F"))
        self.assertIn("no contract specs", str(e.exception))
        with self.assertRaises(size.SizeError):
            sized(plan(symbol="MNQ=F"), blocked=["MNQ"])


def mnq_order(qty="1", **kw):
    s, order = sized(plan(symbol="MNQ=F"), max_loss_per_trade_usd=300, max_contracts=3)
    order = dict(order, qty=qty)
    order.update(kw)
    return order


class Risk(unittest.TestCase):
    def run_check(self, orders, **over):
        return check.check(orders, cfg(**over), now=NOW, today=TODAY)

    def test_futures_are_off_by_default(self):
        r = self.run_check([mnq_order()], max_loss_per_trade_usd=300)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any(x.startswith("future: MNQ=F is a futures contract") for x in r["reasons"]), r["reasons"])

    def test_pass_with_the_loss_in_dollars(self):
        res = self.run_check([mnq_order("2")], allow_futures=True, max_loss_per_trade_usd=300, max_daily_loss_usd=300)
        r = res["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual((r["max_loss"], r["amount"]), (294.0, "2 contracts"))
        self.assertIn("risk 73.5 points, $147 a contract", " ".join(r["info"]))
        self.assertTrue(any("margin not checked" in w for w in r["warnings"]))

    def test_loss_over_the_cap_blocks(self):
        r = self.run_check([mnq_order("2")], allow_futures=True)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("loss: 2 contracts x 73.5 points x $2 a point = $294 at the stop, over your $50 max loss "
                      "per trade", r["reasons"])

    def test_face_value_is_not_counted_as_cash(self):
        # 1 MNQ has a face value near $58,000: without a margin it is not a cash or position problem
        r = self.run_check([mnq_order()], allow_futures=True, max_loss_per_trade_usd=300)["results"][0]
        self.assertFalse([x for x in r["reasons"] if x.startswith(("cash", "position", "size"))], r["reasons"])
        # with a margin, the cash rule uses it
        r = self.run_check([mnq_order()], allow_futures=True, max_loss_per_trade_usd=300,
                           futures_margin_usd={"MNQ": 4800})["results"][0]
        self.assertTrue([x for x in r["reasons"] if x.startswith("cash")], r["reasons"])

    def test_unknown_future_blocks(self):
        r = self.run_check([mnq_order(symbol="ES=F")], allow_futures=True)["results"][0]
        self.assertTrue(any("no contract specs" in x for x in r["reasons"]), r["reasons"])


class Broker(unittest.TestCase):
    def test_a_future_is_a_manual_ticket(self):
        order = mnq_order("2")
        res = check.check([order], cfg(allow_futures=True, max_loss_per_trade_usd=300, max_daily_loss_usd=300),
                          now=NOW, today=TODAY)
        ready, label, rv = ticket.status_of(order, res, {}, TODAY)
        self.assertFalse(ready)
        self.assertTrue(label.startswith("MANUAL: trader-risk PASS"), label)
        self.assertEqual(ticket.max_loss(order), (294.0, 29057.0))
        text, ready = ticket.render(order, 1, 1, cfg(), res, {}, TODAY)
        self.assertIn("BUY 2 MNQ contracts (Micro E-mini Nasdaq-100, front month), market, good for today", text)
        self.assertNotIn("place_stock_order", text)
        j = ticket.ticket_json(order, 1, cfg(), res, {}, TODAY)
        self.assertIsNone(j["tool_input"])

    def test_limit_future_max_loss_uses_the_point_value(self):
        order = mnq_order("1", type="limit", limit_price="29130.50")
        order["plan"] = dict(order["plan"], max_loss=None)
        self.assertEqual(ticket.max_loss(order), (147.0, 29057.0))


if __name__ == "__main__":
    unittest.main()
