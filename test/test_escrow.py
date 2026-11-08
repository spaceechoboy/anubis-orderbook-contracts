import json, os, unittest, warnings
warnings.filterwarnings('ignore')
from web3 import Web3, EthereumTesterProvider
from web3.logs import DISCARD

HERE = os.path.dirname(os.path.abspath(__file__))
ART = json.load(open(os.path.join(HERE, "..", "build", "artifacts.json")))
E18 = 10**18
DAY = 86400

class Base(unittest.TestCase):
    def setUp(self):
        self.w3 = Web3(EthereumTesterProvider())
        self.t = self.w3.provider.ethereum_tester
        a = self.w3.eth.accounts
        self.deployer, self.maker, self.taker, self.third, self.feeRcv = a[0], a[1], a[2], a[3], a[4]
        self.A = self.deploy("MockERC20", "AiLGNS", "AiL", 18)
        self.B = self.deploy("MockERC20", "DAI", "DAI", 18)
        self.esc = self.deploy("P2PEscrow", self.A.address, self.B.address, self.feeRcv, 100)  # 1%
        for who in (self.maker, self.taker, self.third):
            self.A.functions.mint(who, 1000 * E18).transact({"from": self.deployer})
            self.B.functions.mint(who, 1_000_000 * E18).transact({"from": self.deployer})
            self.A.functions.approve(self.esc.address, 2**256 - 1).transact({"from": who})
            self.B.functions.approve(self.esc.address, 2**256 - 1).transact({"from": who})

    def deploy(self, name, *args):
        art = ART[name]
        C = self.w3.eth.contract(abi=art["abi"], bytecode=art["bin"])
        h = C.constructor(*args).transact({"from": self.deployer})
        r = self.w3.eth.wait_for_transaction_receipt(h)
        return self.w3.eth.contract(address=r.contractAddress, abi=art["abi"])

    def bal(self, tok, who): return tok.functions.balanceOf(who).call()
    def now(self): return self.w3.eth.get_block("latest").timestamp
    def warp(self, secs):
        self.t.time_travel(self.now() + secs); self.t.mine_block()

    def expect_revert(self, err, fn, **tx):
        """eth-tester는 custom error를 TransactionFailed(raw bytes)로 올린다. 셀렉터 4바이트를 직접 대조."""
        import ast, re
        with self.assertRaises(Exception) as cm:
            fn.transact(tx)
        sel = Web3.keccak(text=err + "()")[:4]
        msg = str(cm.exception)
        m = re.search(r"reverted: (b['\"].*)$", msg, re.S)
        data = ast.literal_eval(m.group(1).strip()) if m else b""
        if not isinstance(data, (bytes, bytearray)):
            data = bytes.fromhex(str(data).replace("0x", "")) if str(data).strip() else b""
        self.assertEqual(bytes(data[:4]), sel, f"expected {err} ({sel.hex()}), got {msg[:200]}")

    def create(self, sellA, give, want, dur=DAY, minFill=0, taker=None, frm=None):
        frm = frm or self.maker
        h = self.esc.functions.create(sellA, give, want, dur, minFill, taker or "0x" + "0" * 40).transact({"from": frm})
        r = self.w3.eth.wait_for_transaction_receipt(h)
        return self.esc.events.Created().process_receipt(r, errors=DISCARD)[0]["args"]["id"]

    def order(self, i):
        o = self.esc.functions.getOrder(i).call()
        return dict(maker=o[0], taker=o[1], sellA=o[2], open=o[3], expiry=o[4], giveTotal=o[5], wantTotal=o[6], giveLeft=o[7], minFill=o[8])


class TestDeploy(Base):
    def test_params(self):
        self.assertEqual(self.esc.functions.TOKEN_A().call(), self.A.address)
        self.assertEqual(self.esc.functions.FEE_BPS().call(), 100)
        self.assertEqual(self.esc.functions.FEE_RECIPIENT().call(), self.feeRcv)

    def test_zero_fee_when_no_recipient(self):
        e = self.deploy("P2PEscrow", self.A.address, self.B.address, "0x" + "0" * 40, 150)
        self.assertEqual(e.functions.FEE_BPS().call(), 0)

    def test_bad_params(self):
        art = ART["P2PEscrow"]; C = self.w3.eth.contract(abi=art["abi"], bytecode=art["bin"])
        for args in [("0x" + "0" * 40, self.B.address, self.feeRcv, 0), (self.A.address, self.A.address, self.feeRcv, 0), (self.A.address, self.B.address, self.feeRcv, 201)]:
            with self.assertRaises(Exception):
                C.constructor(*args).transact({"from": self.deployer})

    def test_native_rejected(self):
        with self.assertRaises(Exception):
            self.w3.eth.send_transaction({"from": self.maker, "to": self.esc.address, "value": 1})


class TestCreate(Base):
    def test_create_pulls_tokens_and_records(self):
        b0 = self.bal(self.A, self.maker)
        i = self.create(True, 10 * E18, 8000 * E18, minFill=E18)
        self.assertEqual(i, 0)
        self.assertEqual(b0 - self.bal(self.A, self.maker), 10 * E18)
        self.assertEqual(self.bal(self.A, self.esc.address), 10 * E18)
        o = self.order(0)
        self.assertTrue(o["open"]); self.assertEqual(o["giveLeft"], 10 * E18); self.assertEqual(o["minFill"], E18)
        self.assertEqual(self.esc.functions.ordersCount().call(), 1)

    def test_validation(self):
        self.expect_revert("ZeroAmount", self.esc.functions.create(True, 0, 1, DAY, 0, "0x" + "0" * 40), **{"from": self.maker})
        self.expect_revert("ZeroAmount", self.esc.functions.create(True, 1, 0, DAY, 0, "0x" + "0" * 40), **{"from": self.maker})
        self.expect_revert("BadDuration", self.esc.functions.create(True, E18, E18, 599, 0, "0x" + "0" * 40), **{"from": self.maker})
        self.expect_revert("BadDuration", self.esc.functions.create(True, E18, E18, 30 * DAY + 1, 0, "0x" + "0" * 40), **{"from": self.maker})
        self.expect_revert("BadMinFill", self.esc.functions.create(True, E18, E18, DAY, E18 + 1, "0x" + "0" * 40), **{"from": self.maker})

    def test_fee_on_transfer_token_rejected(self):
        F = self.deploy("FeeOnTransferToken")
        e = self.deploy("P2PEscrow", F.address, self.B.address, self.feeRcv, 100)
        F.functions.mint(self.maker, 100 * E18).transact({"from": self.deployer})
        F.functions.approve(e.address, 2**256 - 1).transact({"from": self.maker})
        self.expect_revert("TransferMismatch", e.functions.create(True, 10 * E18, E18, DAY, 0, "0x" + "0" * 40), **{"from": self.maker})


class TestFill(Base):
    def test_full_fill_sell_a(self):
        # maker sells 10 A for 8000 B; taker takes all
        i = self.create(True, 10 * E18, 8000 * E18)
        mB, tA, tB, fA = self.bal(self.B, self.maker), self.bal(self.A, self.taker), self.bal(self.B, self.taker), self.bal(self.A, self.feeRcv)
        h = self.esc.functions.fill(i, 10 * E18, 8000 * E18).transact({"from": self.taker})
        ev = self.esc.events.Filled().process_receipt(self.w3.eth.wait_for_transaction_receipt(h), errors=DISCARD)[0]["args"]
        self.assertEqual(ev["paid"], 8000 * E18); self.assertEqual(ev["fee"], E18 // 10); self.assertEqual(ev["giveLeft"], 0)
        self.assertEqual(self.bal(self.B, self.maker) - mB, 8000 * E18)
        self.assertEqual(tB - self.bal(self.B, self.taker), 8000 * E18)
        self.assertEqual(self.bal(self.A, self.taker) - tA, 10 * E18 - E18 // 10)  # 1% fee
        self.assertEqual(self.bal(self.A, self.feeRcv) - fA, E18 // 10)
        self.assertFalse(self.order(i)["open"]); self.assertEqual(self.bal(self.A, self.esc.address), 0)

    def test_full_fill_buy_a(self):
        # maker deposits 8000 B wanting 10 A
        i = self.create(False, 8000 * E18, 10 * E18)
        mA, tB, fB = self.bal(self.A, self.maker), self.bal(self.B, self.taker), self.bal(self.B, self.feeRcv)
        self.esc.functions.fill(i, 8000 * E18, 10 * E18).transact({"from": self.taker})
        self.assertEqual(self.bal(self.A, self.maker) - mA, 10 * E18)
        self.assertEqual(self.bal(self.B, self.taker) - tB, 8000 * E18 - 80 * E18)
        self.assertEqual(self.bal(self.B, self.feeRcv) - fB, 80 * E18)

    def test_partial_fill_rounds_up_payment(self):
        # price 8000/10 = 800 B per A. take 3 A + 1 wei → pay must round up
        i = self.create(True, 10 * E18, 8000 * E18)
        amt = 3 * E18 + 1
        pay, fee, recv = self.esc.functions.quote(i, amt).call()
        self.assertEqual(pay, (amt * 8000 * E18 + 10 * E18 - 1) // (10 * E18))
        self.assertGreater(pay, 2400 * E18)
        self.esc.functions.fill(i, amt, pay).transact({"from": self.taker})
        self.assertEqual(self.order(i)["giveLeft"], 10 * E18 - amt)
        self.assertTrue(self.order(i)["open"])
        # second taker finishes it
        left = self.order(i)["giveLeft"]
        self.esc.functions.fill(i, left, 2**256 - 1).transact({"from": self.third})
        self.assertFalse(self.order(i)["open"]); self.assertEqual(self.bal(self.A, self.esc.address), 0)

    def test_max_pay_guard(self):
        i = self.create(True, 10 * E18, 8000 * E18)
        self.expect_revert("PriceChanged", self.esc.functions.fill(i, E18, 800 * E18 - 1), **{"from": self.taker})

    def test_min_fill_and_dust_rules(self):
        i = self.create(True, 10 * E18, 8000 * E18, minFill=2 * E18)
        self.expect_revert("BelowMinFill", self.esc.functions.fill(i, E18, 2**256 - 1), **{"from": self.taker})       # too small
        self.expect_revert("DustLeft", self.esc.functions.fill(i, 9 * E18, 2**256 - 1), **{"from": self.taker})       # leaves 1 < minFill
        self.esc.functions.fill(i, 8 * E18, 2**256 - 1).transact({"from": self.taker})                                # leaves exactly 2
        self.esc.functions.fill(i, 2 * E18, 2**256 - 1).transact({"from": self.third})                                # take all remaining (allowed)
        self.assertFalse(self.order(i)["open"])

    def test_take_all_remaining_below_min_fill_allowed(self):
        i = self.create(True, 10 * E18, 8000 * E18, minFill=4 * E18)
        self.esc.functions.fill(i, 6 * E18, 2**256 - 1).transact({"from": self.taker})  # leaves 4 (== minFill ok)
        self.esc.functions.fill(i, 4 * E18, 2**256 - 1).transact({"from": self.third})
        self.assertFalse(self.order(i)["open"])

    def test_fill_by_pay(self):
        i = self.create(True, 10 * E18, 8000 * E18)  # 800 B per A
        tA = self.bal(self.A, self.taker); tB = self.bal(self.B, self.taker)
        self.esc.functions.fillByPay(i, 400 * E18, 495 * E18 // 1000).transact({"from": self.taker})  # 0.5 gross → 0.495 net A
        self.assertEqual(tB - self.bal(self.B, self.taker), 400 * E18)
        self.assertEqual(self.bal(self.A, self.taker) - tA, E18 // 2 - (E18 // 2) // 100)
        self.expect_revert("PriceChanged", self.esc.functions.fillByPay(i, 400 * E18, E18), **{"from": self.taker})  # minReceive too high
        # overpay beyond remaining is clamped to giveLeft
        left = self.order(i)["giveLeft"]
        self.esc.functions.fillByPay(i, 10**9 * E18, left - left // 100).transact({"from": self.third})
        self.assertFalse(self.order(i)["open"])

    def test_fill_by_pay_min_receive_guards_net_after_fee(self):
        i = self.create(True, 10 * E18, 8000 * E18)
        before = self.bal(self.B, self.taker)
        self.expect_revert("PriceChanged", self.esc.functions.fillByPay(i, 400 * E18, E18 // 2), **{"from": self.taker})
        self.assertEqual(self.order(i)["giveLeft"], 10 * E18)
        self.assertEqual(self.bal(self.B, self.taker), before)
        received = self.bal(self.A, self.taker)
        self.esc.functions.fillByPay(i, 400 * E18, 495 * E18 // 1000).transact({"from": self.taker})
        self.assertEqual(self.bal(self.A, self.taker) - received, 495 * E18 // 1000)

    def test_fill_by_pay_enforces_min_fill_and_dust(self):
        i = self.create(True, 10 * E18, 8000 * E18, minFill=2 * E18)
        self.expect_revert("BelowMinFill", self.esc.functions.fillByPay(i, 800 * E18, 0), **{"from": self.taker})
        self.expect_revert("DustLeft", self.esc.functions.fillByPay(i, 7200 * E18, 0), **{"from": self.taker})
        self.assertEqual(self.order(i)["giveLeft"], 10 * E18)
        self.esc.functions.fillByPay(i, 6400 * E18, 792 * E18 // 100).transact({"from": self.taker})
        self.assertEqual(self.order(i)["giveLeft"], 2 * E18)
        self.esc.functions.fillByPay(i, 1600 * E18, 198 * E18 // 100).transact({"from": self.third})
        self.assertFalse(self.order(i)["open"])
        self.assertEqual(self.bal(self.A, self.esc.address), 0)

    def test_fill_by_pay_rounds_payment_within_budget_and_clamps(self):
        i = self.create(True, 3, 10)
        before = self.bal(self.B, self.taker)
        self.esc.functions.fillByPay(i, 4, 1).transact({"from": self.taker})
        self.assertEqual(before - self.bal(self.B, self.taker), 4)
        self.assertEqual(self.order(i)["giveLeft"], 2)
        before = self.bal(self.B, self.third)
        self.esc.functions.fillByPay(i, 99, 2).transact({"from": self.third})
        self.assertEqual(before - self.bal(self.B, self.third), 7)
        self.assertFalse(self.order(i)["open"])

    def test_private_order_taker_restriction(self):
        i = self.create(True, E18, 800 * E18, taker=self.third)
        self.expect_revert("NotTaker", self.esc.functions.fill(i, E18, 2**256 - 1), **{"from": self.taker})
        self.esc.functions.fill(i, E18, 2**256 - 1).transact({"from": self.third})
        self.assertFalse(self.order(i)["open"])

    def test_self_fill_rejected(self):
        i = self.create(True, E18, 800 * E18)
        self.expect_revert("SelfFill", self.esc.functions.fill(i, E18, 2**256 - 1), **{"from": self.maker})

    def test_fill_closed_or_missing(self):
        i = self.create(True, E18, 800 * E18)
        self.esc.functions.cancel(i).transact({"from": self.maker})
        self.expect_revert("Closed", self.esc.functions.fill(i, E18, 2**256 - 1), **{"from": self.taker})
        self.expect_revert("NoOrder", self.esc.functions.fill(99, E18, 2**256 - 1), **{"from": self.taker})

    def test_zero_fee_mode(self):
        e = self.deploy("P2PEscrow", self.A.address, self.B.address, "0x" + "0" * 40, 0)
        self.A.functions.approve(e.address, 2**256 - 1).transact({"from": self.maker})
        self.B.functions.approve(e.address, 2**256 - 1).transact({"from": self.taker})
        e.functions.create(True, E18, 800 * E18, DAY, 0, "0x" + "0" * 40).transact({"from": self.maker})
        tA = self.bal(self.A, self.taker)
        e.functions.fill(0, E18, 2**256 - 1).transact({"from": self.taker})
        self.assertEqual(self.bal(self.A, self.taker) - tA, E18)


class TestCancelExpire(Base):
    def test_blocked_refund_reverts_whole_batch_but_other_order_can_refund_alone(self):
        tok = self.deploy("BlockedReceiverToken")
        self.esc = self.deploy("P2PEscrow", tok.address, self.B.address, self.feeRcv, 100)
        for who in (self.maker, self.third):
            tok.functions.mint(who, 10 * E18).transact({"from": self.deployer})
            tok.functions.approve(self.esc.address, 10 * E18).transact({"from": who})
        good = self.create(True, E18, 800 * E18, dur=600, frm=self.third)
        bad = self.create(True, E18, 800 * E18, dur=600)
        tok.functions.setBlocked(self.maker, True).transact({"from": self.deployer})
        self.warp(700)
        before = self.bal(tok, self.third)
        self.expect_revert("TransferFailed", self.esc.functions.refundExpired([good, bad]), **{"from": self.taker})
        self.assertTrue(self.order(good)["open"])
        self.assertTrue(self.order(bad)["open"])
        self.assertEqual(self.bal(tok, self.third), before)
        self.assertEqual(self.bal(tok, self.esc.address), 2 * E18)
        self.esc.functions.refundExpired([good]).transact({"from": self.taker})
        self.assertFalse(self.order(good)["open"])
        self.assertTrue(self.order(bad)["open"])
        self.assertEqual(self.bal(tok, self.third), before + E18)
        self.assertEqual(self.bal(tok, self.esc.address), E18)

    def test_cancel_refunds_remaining(self):
        i = self.create(True, 10 * E18, 8000 * E18)
        self.esc.functions.fill(i, 3 * E18, 2**256 - 1).transact({"from": self.taker})
        mA = self.bal(self.A, self.maker)
        self.expect_revert("NotMaker", self.esc.functions.cancel(i), **{"from": self.taker})
        h = self.esc.functions.cancel(i).transact({"from": self.maker})
        ev = self.esc.events.Cancelled().process_receipt(self.w3.eth.wait_for_transaction_receipt(h), errors=DISCARD)[0]["args"]
        self.assertEqual(ev["refunded"], 7 * E18); self.assertEqual(self.bal(self.A, self.maker) - mA, 7 * E18)
        self.expect_revert("Closed", self.esc.functions.cancel(i), **{"from": self.maker})
        self.assertEqual(self.bal(self.A, self.esc.address), 0)

    def test_expired_cannot_fill_and_anyone_can_refund(self):
        i = self.create(True, 10 * E18, 8000 * E18, dur=600)
        j = self.create(False, 800 * E18, E18, dur=DAY)
        self.warp(601)
        self.expect_revert("OrderExpired", self.esc.functions.fill(i, E18, 2**256 - 1), **{"from": self.taker})
        mA, mB = self.bal(self.A, self.maker), self.bal(self.B, self.maker)
        h = self.esc.functions.refundExpired([i, j, 999]).transact({"from": self.third})  # j not expired, 999 missing → skipped
        evs = self.esc.events.Expired().process_receipt(self.w3.eth.wait_for_transaction_receipt(h), errors=DISCARD)
        self.assertEqual(len(evs), 1); self.assertEqual(evs[0]["args"]["id"], i); self.assertEqual(evs[0]["args"]["refunded"], 10 * E18)
        self.assertEqual(self.bal(self.A, self.maker) - mA, 10 * E18)   # refund went to maker, not caller
        self.assertEqual(self.bal(self.B, self.maker), mB)             # j untouched
        self.assertTrue(self.order(j)["open"]); self.assertFalse(self.order(i)["open"])
        self.esc.functions.refundExpired([i]).transact({"from": self.third})  # idempotent

    def test_maker_can_cancel_after_expiry_too(self):
        i = self.create(True, E18, 800 * E18, dur=600)
        self.warp(700)
        self.esc.functions.cancel(i).transact({"from": self.maker})
        self.assertFalse(self.order(i)["open"])


class TestReentrancy(Base):
    def setUp(self):
        super().setUp()
        self.EV = self.deploy("ReentrantToken")
        self.esc2 = self.deploy("P2PEscrow", self.EV.address, self.B.address, self.feeRcv, 100)
        for who in (self.maker, self.taker):
            self.EV.functions.mint(who, 100 * E18).transact({"from": self.deployer})
            self.EV.functions.approve(self.esc2.address, 2**256 - 1).transact({"from": who})
            self.B.functions.approve(self.esc2.address, 2**256 - 1).transact({"from": who})

    def _create(self):
        h = self.esc2.functions.create(True, 10 * E18, 8000 * E18, DAY, 0, "0x" + "0" * 40).transact({"from": self.maker})
        return self.esc2.events.Created().process_receipt(self.w3.eth.wait_for_transaction_receipt(h), errors=DISCARD)[0]["args"]["id"]

    def _assert_blocked(self):
        self.assertTrue(self.EV.functions.fired().call())
        self.assertFalse(self.EV.functions.reentrySucceeded().call())
        self.assertEqual(self.EV.functions.lastErr().call(), Web3.keccak(text="Reentrancy()")[:4])

    def test_reenter_cancel_during_fill_blocked(self):
        i = self._create()
        self.EV.functions.arm(self.esc2.address, 1, i).transact({"from": self.maker})
        tEV = self.bal(self.EV, self.taker)
        self.esc2.functions.fill(i, E18, 2**256 - 1).transact({"from": self.taker})  # 바깥 체결은 정상 완료
        self._assert_blocked()
        self.assertEqual(self.esc2.functions.getOrder(i).call()[7], 9 * E18)        # 재진입 cancel이 먹히지 않았음(잔량 9, 아직 open)
        self.assertTrue(self.esc2.functions.getOrder(i).call()[3])
        self.assertEqual(self.bal(self.EV, self.taker) - tEV, E18 - E18 // 100)
        self.assertEqual(self.bal(self.EV, self.esc2.address), 9 * E18)              # 장부 = 잔액

    def test_reenter_fill_during_fill_blocked(self):
        i = self._create()
        self.EV.functions.arm(self.esc2.address, 2, i).transact({"from": self.maker})
        self.esc2.functions.fill(i, E18, 2**256 - 1).transact({"from": self.taker})
        self._assert_blocked()
        self.assertEqual(self.esc2.functions.getOrder(i).call()[7], 9 * E18)

    def test_reenter_refund_during_cancel_blocked(self):
        i = self._create()
        self.EV.functions.arm(self.esc2.address, 3, i).transact({"from": self.maker})
        mEV = self.bal(self.EV, self.maker)
        self.esc2.functions.cancel(i).transact({"from": self.maker})
        self._assert_blocked()
        self.assertEqual(self.bal(self.EV, self.maker) - mEV, 10 * E18)              # 환불은 정확히 1회
        self.assertEqual(self.bal(self.EV, self.esc2.address), 0)

class TestSenderTax(Base):
    def setUp(self):
        super().setUp()
        self.tax = self.deploy("SenderTaxToken")

    def test_extra_sender_tax_is_rejected_on_deposit(self):
        self.esc = self.deploy("P2PEscrow", self.tax.address, self.B.address, self.feeRcv, 100)
        self.tax.functions.mint(self.maker, 100 * E18).transact({"from": self.deployer})
        self.tax.functions.approve(self.esc.address, 100 * E18).transact({"from": self.maker})
        self.tax.functions.setTaxed(True).transact({"from": self.deployer})
        self.expect_revert("TransferMismatch", self.esc.functions.create(True, 10 * E18, 8000 * E18, DAY, 0, "0x" + "0" * 40), **{"from": self.maker})
        self.assertEqual(self.bal(self.tax, self.maker), 100 * E18)
        self.assertEqual(self.bal(self.tax, self.esc.address), 0)

    def test_extra_sender_tax_cannot_consume_other_makers_collateral(self):
        self.esc = self.deploy("P2PEscrow", self.tax.address, self.B.address, self.feeRcv, 100)
        for who in (self.maker, self.third):
            self.tax.functions.mint(who, 100 * E18).transact({"from": self.deployer})
            self.tax.functions.approve(self.esc.address, 100 * E18).transact({"from": who})
        self.B.functions.approve(self.esc.address, 100000 * E18).transact({"from": self.taker})
        first = self.create(True, 10 * E18, 8000 * E18)
        other = self.create(True, 10 * E18, 8000 * E18, frm=self.third)
        self.tax.functions.setTaxed(True).transact({"from": self.deployer})
        self.expect_revert("TransferMismatch", self.esc.functions.fill(first, 10 * E18, 8000 * E18), **{"from": self.taker})
        self.assertEqual(self.order(first)["giveLeft"], 10 * E18)
        self.assertEqual(self.order(other)["giveLeft"], 10 * E18)
        self.assertEqual(self.bal(self.tax, self.esc.address), 20 * E18)
        self.tax.functions.setTaxed(False).transact({"from": self.deployer})
        self.esc.functions.cancel(other).transact({"from": self.third})
        self.assertEqual(self.bal(self.tax, self.third), 100 * E18)


class TestViews(Base):
    def test_get_orders_paging(self):
        for k in range(5): self.create(True, E18, (800 + k) * E18)
        self.assertEqual(len(self.esc.functions.getOrders(0, 3).call()), 3)
        self.assertEqual(len(self.esc.functions.getOrders(3, 10).call()), 2)
        self.assertEqual(len(self.esc.functions.getOrders(7, 3).call()), 0)
        self.assertEqual(self.esc.functions.getOrders(4, 1).call()[0][6], 804 * E18)
        with self.assertRaises(Exception): self.esc.functions.getOrder(5).call()

    def test_invariant_contract_balance_equals_open_give(self):
        ids = [self.create(True, (k + 1) * E18, 800 * (k + 1) * E18) for k in range(4)]
        self.create(False, 1600 * E18, 2 * E18)
        self.esc.functions.fill(ids[1], E18, 2**256 - 1).transact({"from": self.taker})
        self.esc.functions.cancel(ids[2]).transact({"from": self.maker})
        self.esc.functions.fillByPay(4, 800 * E18, 0).transact({"from": self.taker})
        sumA = sumB = 0
        for k in range(self.esc.functions.ordersCount().call()):
            o = self.order(k)
            if o["open"]:
                if o["sellA"]: sumA += o["giveLeft"]
                else: sumB += o["giveLeft"]
        self.assertEqual(self.bal(self.A, self.esc.address), sumA)
        self.assertEqual(self.bal(self.B, self.esc.address), sumB)

if __name__ == "__main__":
    unittest.main(verbosity=2)
