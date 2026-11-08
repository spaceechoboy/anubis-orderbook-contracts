import ast
import re

from web3 import EthereumTesterProvider, Web3
from web3.logs import DISCARD

from test_escrow import ART, Base, DAY, E18


UNIT = 10**6
LOT = 10**12
ZERO = "0x" + "0" * 40


class OrderBookFixture(Base):
    with_fee = False
    fixed_fee = False

    def setUp(self):
        self.w3 = Web3(EthereumTesterProvider())
        self.t = self.w3.provider.ethereum_tester
        self.deployer, self.maker, self.taker, self.third, self.fourth, self.fee_recipient = self.w3.eth.accounts[:6]
        self.A = self.deploy("BlockedReceiverToken")
        self.B = self.deploy("BlockedReceiverToken")
        recipient = self.fee_recipient if self.with_fee else ZERO
        self.authority = self.deploy("FeeAuthorityHarness") if self.with_fee and not self.fixed_fee else None
        self.book = self.deploy("P2POrderBook", self.A.address, self.B.address, recipient,
                                self.authority.address if self.authority else ZERO,
                                100 if self.with_fee else 0, (100 if self.fixed_fee else 200) if self.with_fee else 0,
                                3600 if self.with_fee and not self.fixed_fee else 0)
        for who in (self.maker, self.taker, self.third, self.fourth):
            self.A.functions.mint(who, 10 * E18).transact()
            self.B.functions.mint(who, 10_000 * E18).transact()
            self.A.functions.approve(self.book.address, 10 * E18).transact({"from": who})
            self.B.functions.approve(self.book.address, 10_000 * E18).transact({"from": who})

    def place(self, owner, buy, price=800, lots=UNIT, after=0, duration=DAY):
        rate = self.book.functions.activeFeeBps().call()
        return self.send(self.book.functions.placeLimit(buy, price * UNIT, lots, duration, after, rate), owner)

    def market(self, buy, price, lots, quote_bound):
        rate = self.book.functions.activeFeeBps().call()
        return self.book.functions.takeMarket(buy, price, lots, quote_bound, rate)

    def authority_function(self, function):
        data = self.book.encode_abi(function.fn_name, args=function.args)
        return self.authority.functions.forward(self.book.address, data)

    def authority_call(self, function):
        return self.send(self.authority_function(function), self.deployer)

    def send(self, function, owner):
        return self.w3.eth.wait_for_transaction_receipt(function.transact({"from": owner}))

    def trades(self, receipt):
        return [event["args"] for event in self.book.events.Trade().process_receipt(receipt, errors=DISCARD)]


class TestOrderBook(OrderBookFixture):
    def test_book_changes_cover_registration_partial_fill_and_pruned_expiry(self):
        placed = self.place(self.maker, False, 800, duration=600)
        partial = self.place(self.taker, True, 800, lots=500_000)
        self.warp(601)
        pruned = self.send(self.book.functions.advance(), self.third)
        changes = self.book.events.BookOrderChanged()
        actual = [(log["args"]["sequence"], log["args"]["id"], log["args"]["listed"])
                  for tx in (placed, partial, pruned)
                  for log in changes.process_receipt(tx, errors=DISCARD)]
        self.assertEqual(actual, [(1, 1, True), (2, 2, True), (3, 2, False), (4, 1, True), (5, 1, False)])
        self.assertEqual(self.book.functions.bookMutationCount().call(), 5)

    def test_audit_pages_include_pruned_expiry_and_closed_orders(self):
        self.place(self.maker, False, 800, duration=600)
        self.place(self.third, False, 801, after=1)
        self.warp(601)
        self.send(self.book.functions.advance(), self.taker)
        self.send(self.book.functions.cancel(2), self.third)
        first, = self.book.functions.getOrdersById(1, 1).call()
        second, = self.book.functions.getOrdersById(2, 64).call()
        self.assertEqual((first[0], first[3]), (self.maker, 0))
        self.assertEqual((second[0], second[3]), (self.third, 2))
        self.assertEqual(self.book.functions.getOrdersById(3, 64).call(), [])
        self.expect_revert("BadPageSize", self.book.functions.getOrdersById(1, 65), **{"from": self.taker})

    def test_equal_price_matches_both_deposits_in_second_order_transaction(self):
        self.assertEqual(self.book.functions.activeFeeBps().call(), 0)
        self.place(self.maker, False)
        receipt = self.place(self.taker, True, lots=UNIT // 2)
        fill, = self.trades(receipt)
        self.assertEqual((fill["bidId"], fill["askId"], fill["lots"], fill["quote"]), (2, 1, 500_000, 400 * E18))
        self.assertEqual(self.bal(self.A, self.taker), 10 * E18 + E18 // 2)
        self.assertEqual(self.bal(self.B, self.maker), 10_400 * E18)
        self.assertEqual(self.bal(self.A, self.book.address), E18 // 2)
        self.assertEqual(self.bal(self.B, self.book.address), 0)
        ids, _ = self.book.functions.getBook(False, 0, 32).call()
        self.assertEqual(ids, [1])
        self.send(self.book.functions.cancel(1), self.maker)
        self.assertEqual(self.bal(self.A, self.maker), 10 * E18 - E18 // 2)
        self.assertEqual(self.bal(self.A, self.book.address), 0)

    def test_best_price_and_fifo_use_exact_on_chain_priority(self):
        self.place(self.maker, False, 801, lots=500_000)
        self.place(self.third, False, 799, lots=250_000)
        self.expect_revert("BadPosition", self.book.functions.placeLimit(False, 799 * UNIT, 250_000, DAY, 0, 0), **{"from": self.fourth})
        self.place(self.fourth, False, 799, lots=250_000, after=2)
        receipt = self.place(self.taker, True, 805, lots=750_000)
        self.assertEqual([(e["askId"], e["price"], e["lots"]) for e in self.trades(receipt)],
                         [(2, 799 * UNIT, 250_000), (3, 799 * UNIT, 250_000), (1, 801 * UNIT, 250_000)])
        self.assertEqual(self.bal(self.B, self.taker), 10_000 * E18 - 599_750 * 10**15)
        self.assertEqual(self.bal(self.B, self.book.address), 0)

    def test_resting_bid_price_and_remaining_bid_reserve(self):
        self.place(self.maker, True, 800)
        receipt = self.place(self.taker, False, 790, lots=500_000)
        self.assertEqual(self.trades(receipt)[0]["quote"], 400 * E18)
        self.assertEqual(self.bal(self.B, self.book.address), 400 * E18)
        self.expect_revert("NotMaker", self.book.functions.cancel(1), **{"from": self.taker})
        self.send(self.book.functions.cancel(1), self.maker)
        self.assertEqual(self.bal(self.B, self.maker), 9_600 * E18)
        self.assertEqual(self.bal(self.A, self.maker), 10 * E18 + E18 // 2)

    def test_batch_market_bound_rolls_back_then_fills_two_orders_in_one_transaction(self):
        self.place(self.maker, False, 799, lots=500_000)
        self.place(self.third, False, 801, lots=500_000, after=1)
        market = self.market
        self.expect_revert("PriceChanged", market(True, 801 * UNIT, UNIT, 800 * E18 - 1), **{"from": self.taker})
        self.expect_revert("NotEnoughLiquidity", market(True, 801 * UNIT, UNIT + 1, 801 * E18), **{"from": self.taker})
        self.assertEqual(self.book.functions.orderCount().call(), 2)
        self.assertEqual(self.bal(self.A, self.book.address), E18)
        receipt = self.send(market(True, 801 * UNIT, UNIT, 800 * E18), self.taker)
        self.assertEqual([(e["askId"], e["quote"]) for e in self.trades(receipt)], [(1, 3995 * E18 // 10), (2, 4005 * E18 // 10)])
        self.assertEqual(self.bal(self.A, self.taker), 11 * E18)
        self.assertEqual(self.bal(self.B, self.taker), 9_200 * E18)

    def test_sell_market_minimum_proceeds_and_price_floor(self):
        self.place(self.maker, True, 804, lots=500_000)
        self.place(self.third, True, 800, lots=500_000, after=1)
        market = self.market
        self.expect_revert("PriceChanged", market(False, 800 * UNIT, UNIT, 803 * E18), **{"from": self.taker})
        self.expect_revert("NotEnoughLiquidity", market(False, 801 * UNIT, UNIT, 802 * E18), **{"from": self.taker})
        receipt = self.send(market(False, 800 * UNIT, UNIT, 802 * E18), self.taker)
        self.assertEqual(len(self.trades(receipt)), 2)
        self.assertEqual(self.bal(self.B, self.taker), 10_802 * E18)
        self.assertEqual(self.bal(self.B, self.book.address), 0)

    def test_expired_order_is_skipped_and_anyone_can_refund_only_to_maker(self):
        self.place(self.maker, False, 799, duration=600)
        self.place(self.third, False, 800, after=1)
        self.expect_revert("NotExpired", self.book.functions.refundExpired(1), **{"from": self.taker})
        self.warp(601)
        receipt = self.place(self.taker, True, 800)
        self.assertEqual([e["askId"] for e in self.trades(receipt)], [2])
        self.send(self.book.functions.refundExpired(1), self.taker)
        self.assertEqual(self.bal(self.A, self.maker), 10 * E18)
        self.assertEqual(self.bal(self.A, self.taker), 11 * E18)
        self.expect_revert("Closed", self.book.functions.refundExpired(1), **{"from": self.taker})

    def test_self_cross_returns_new_order_without_printing_trade(self):
        self.place(self.maker, False)
        receipt = self.place(self.maker, True)
        self.assertEqual(self.trades(receipt), [])
        self.assertEqual(self.bal(self.B, self.maker), 10_000 * E18)
        self.assertEqual(self.book.functions.getBook(False, 0, 1).call()[0], [1])
        self.expect_revert("SelfTrade", self.market(True, 800 * UNIT, UNIT, 800 * E18), **{"from": self.maker})

    def test_blocked_payout_cannot_freeze_top_of_book_or_be_redirected(self):
        self.place(self.maker, False)
        self.A.functions.setBlocked(self.taker, True).transact()
        receipt = self.place(self.taker, True)
        self.assertEqual(len(self.trades(receipt)), 1)
        self.assertEqual(self.bal(self.B, self.maker), 10_800 * E18)
        credit = self.book.functions.claimable(self.A.address, self.taker)
        self.assertEqual(credit.call(), E18)
        self.assertEqual(self.book.functions.deferredPayoutCount().call(), 1)
        withdraw = self.book.functions.withdraw(self.A.address, self.taker)
        self.expect_revert("TransferFailed", withdraw, **{"from": self.third})
        self.assertEqual(credit.call(), E18)
        self.A.functions.setBlocked(self.taker, False).transact()
        self.send(withdraw, self.third)
        self.assertEqual(credit.call(), 0)
        self.assertEqual(self.book.functions.deferredPayoutCount().call(), 1)
        self.assertEqual(self.bal(self.A, self.third), 10 * E18)
        self.assertEqual(self.bal(self.A, self.taker), 11 * E18)

    def test_bounded_match_continues_without_executor_privileges(self):
        for i in range(18):
            self.place(self.maker, False, lots=10_000, after=i)
        receipt = self.place(self.taker, True, lots=180_000)
        self.assertEqual(len(self.trades(receipt)), 16)
        self.assertTrue(self.book.functions.needsAdvance().call())
        receipt = self.send(self.book.functions.advance(), self.third)
        self.assertEqual([e["askId"] for e in self.trades(receipt)], [17, 18])
        self.assertFalse(self.book.functions.needsAdvance().call())
        self.assertEqual(self.bal(self.A, self.book.address), 0)
        self.assertEqual(self.bal(self.B, self.book.address), 0)

    def test_sender_tax_on_payout_keeps_other_collateral_and_credits_exact_amount(self):
        self.A = self.deploy("SenderTaxToken")
        self.book = self.deploy("P2POrderBook", self.A.address, self.B.address, ZERO, ZERO, 0, 0, 0)
        self.A.functions.mint(self.maker, 10 * E18).transact()
        self.A.functions.approve(self.book.address, 2 * E18).transact({"from": self.maker})
        self.B.functions.approve(self.book.address, 800 * E18).transact({"from": self.taker})
        self.place(self.maker, False)
        self.place(self.maker, False, 801, after=1)
        self.A.functions.setTaxed(True).transact()
        self.place(self.taker, True)
        self.assertEqual(self.bal(self.A, self.book.address), 2 * E18)
        self.assertEqual(self.bal(self.A, Web3.to_checksum_address("0x" + "0" * 36 + "dead")), 0)
        self.assertEqual(self.book.functions.claimable(self.A.address, self.taker).call(), E18)
        self.assertEqual(self.bal(self.B, self.maker), 10_800 * E18)
        self.A.functions.setTaxed(False).transact()
        self.send(self.book.functions.withdraw(self.A.address, self.taker), self.taker)
        self.assertEqual(self.bal(self.A, self.book.address), E18)
        self.assertEqual(self.bal(self.A, self.taker), E18)

    def test_market_reverts_when_taker_cannot_receive_exact_payout(self):
        self.place(self.maker, False)
        self.A.functions.setBlocked(self.taker, True).transact()
        take = self.market(True, 800 * UNIT, UNIT, 800 * E18)
        self.expect_revert("TransferFailed", take, **{"from": self.taker})
        self.assertEqual(self.book.functions.orderCount().call(), 1)
        self.assertEqual(self.bal(self.A, self.book.address), E18)
        self.assertEqual(self.bal(self.B, self.taker), 10_000 * E18)
        self.assertEqual(self.book.functions.claimable(self.A.address, self.taker).call(), 0)

    def test_market_step_limit_leaves_dust_unchanged_and_limit_order_continues(self):
        for i in range(16):
            self.place(self.maker, False, lots=1, after=i)
        self.place(self.maker, False, 801, after=16)
        take = self.market(True, 801 * UNIT, UNIT, 801 * E18)
        self.expect_revert("NotEnoughLiquidity", take, **{"from": self.taker})
        self.assertEqual(self.book.functions.orderCount().call(), 17)
        self.assertEqual(self.bal(self.A, self.book.address), E18 + 16 * LOT)
        self.assertEqual(len(self.trades(self.place(self.taker, True, 801))), 16)
        self.assertEqual(len(self.trades(self.send(self.book.functions.advance(), self.third))), 1)
        self.assertEqual(self.bal(self.A, self.taker), 11 * E18)

    def test_external_caller_cannot_send_escrow_tokens_to_arbitrary_recipient(self):
        self.place(self.maker, False)
        deliver = self.book.functions.deliver(self.A.address, self.third, E18)
        self.expect_revert("OnlySelf", deliver, **{"from": self.third})
        self.assertEqual(self.bal(self.A, self.book.address), E18)

    def test_order_limits_and_cancelled_head_do_not_match(self):
        self.place(self.maker, False, 805)
        self.place(self.taker, True, 804)
        self.send(self.book.functions.cancel(1), self.maker)
        receipt = self.place(self.third, False, 804)
        fill, = self.trades(receipt)
        self.assertEqual((fill["bidId"], fill["askId"]), (2, 3))
        self.assertEqual(self.bal(self.B, self.maker), 10_000 * E18)


class TestOrderBookFee(OrderBookFixture):
    with_fee = True

    def test_receipt_totals_and_maker_history_survive_partial_and_final_fills(self):
        first = self.place(self.maker, False, 800)
        second = self.place(self.maker, False, 810, after=1)
        self.place(self.taker, True, 820, lots=1_500_000)
        open_order, gross, fee, placed_block = self.book.functions.getOrderReceipt(2).call()
        self.assertEqual((open_order[6], gross, fee, placed_block), (500_000, 405 * E18, 0, second.blockNumber))
        self.assertEqual(self.book.functions.getOrderReceipt(1).call()[1:], [800 * E18, 0, first.blockNumber])
        self.assertEqual(self.book.functions.getOrderReceipt(3).call()[1:3], [1205 * E18, 1205 * E18 // 100])
        self.place(self.third, True, 810, lots=500_000)
        closed_order, gross, fee, placed_block = self.book.functions.getOrderReceipt(2).call()
        self.assertEqual((closed_order[3], gross, fee, placed_block), (1, 810 * E18, 0, second.blockNumber))
        ids, orders = self.book.functions.getRecentOrders(self.maker, 2).call()
        self.assertEqual(ids, [2, 1])
        self.assertEqual([order[3] for order in orders], [1, 1])
        self.assertEqual(self.book.functions.getRecentOrders(self.fourth, 2).call(), [[], []])

    def test_later_limit_buy_pays_dai_fee_on_actual_execution_price(self):
        self.assertEqual(self.book.functions.activeFeeBps().call(), 100)
        self.assertEqual(self.book.functions.FEE_RECIPIENT().call(), self.fee_recipient)
        self.place(self.maker, False, 800)
        receipt = self.place(self.taker, True, 810, lots=500_000)
        fill, = self.trades(receipt)
        self.assertEqual((fill["price"], fill["quote"], fill["fee"]), (800 * UNIT, 400 * E18, 4 * E18))
        self.assertEqual(self.bal(self.B, self.taker), 9_596 * E18)
        self.assertEqual(self.bal(self.B, self.maker), 10_400 * E18)
        self.assertEqual(self.bal(self.B, self.fee_recipient), 4 * E18)
        self.assertEqual(self.bal(self.A, self.taker), 10 * E18 + E18 // 2)
        self.assertEqual(self.bal(self.A, self.book.address), E18 // 2)
        self.assertEqual(self.bal(self.B, self.book.address), 0)

    def test_later_limit_sell_pays_fee_and_buyer_gets_unused_fee_reserve(self):
        self.place(self.maker, True, 800)
        self.assertEqual(self.bal(self.B, self.book.address), 808 * E18)
        receipt = self.place(self.taker, False, 790, lots=500_000)
        self.assertEqual(self.trades(receipt)[0]["fee"], 4 * E18)
        self.assertEqual(self.bal(self.B, self.taker), 10_396 * E18)
        self.assertEqual(self.bal(self.B, self.maker), 9_196 * E18)
        self.assertEqual(self.bal(self.B, self.book.address), 404 * E18)
        self.assertEqual(self.bal(self.B, self.fee_recipient), 4 * E18)
        self.send(self.book.functions.cancel(1), self.maker)
        self.assertEqual(self.bal(self.B, self.maker), 9_600 * E18)
        self.assertEqual(self.bal(self.A, self.maker), 10 * E18 + E18 // 2)
        self.assertEqual(self.bal(self.B, self.book.address), 0)

    def test_market_buy_uses_fee_in_bound_and_has_no_fixed_dai_cap(self):
        self.place(self.maker, False, 2_999, lots=500_000)
        self.place(self.third, False, 3_001, lots=500_000, after=1)
        market = self.market
        over_bound = market(True, 3_001 * UNIT, UNIT, 3_030 * E18 - 1)
        self.expect_revert("PriceChanged", over_bound, **{"from": self.taker})
        failed = self.w3.eth.wait_for_transaction_receipt(over_bound.transact({"from": self.taker, "gas": 1_000_000}))
        self.assertEqual((failed.status, len(failed.logs)), (0, 0))
        self.assertEqual(self.book.functions.orderCount().call(), 2)
        self.assertEqual(self.book.functions.getBook(False, 0, 2).call()[0], [1, 2])
        self.assertEqual(self.bal(self.A, self.book.address), E18)
        self.assertEqual(self.bal(self.B, self.book.address), 0)
        self.assertEqual(self.bal(self.B, self.taker), 10_000 * E18)
        self.assertEqual(self.bal(self.B, self.fee_recipient), 0)
        receipt = self.send(market(True, 3_001 * UNIT, UNIT, 3_030 * E18), self.taker)
        self.assertEqual(len(self.trades(receipt)), 2)
        self.assertEqual(self.bal(self.B, self.fee_recipient), 30 * E18)
        self.assertEqual(self.bal(self.B, self.taker), 6_970 * E18)
        self.assertEqual(self.bal(self.A, self.taker), 11 * E18)

    def test_market_sell_floor_applies_after_fee(self):
        self.place(self.maker, True, 801, lots=500_000)
        self.place(self.third, True, 799, lots=500_000, after=1)
        market = self.market
        self.expect_revert("PriceChanged", market(False, 799 * UNIT, UNIT, 792 * E18 + 1), **{"from": self.taker})
        self.assertEqual(self.bal(self.B, self.book.address), 808 * E18)
        receipt = self.send(market(False, 799 * UNIT, UNIT, 792 * E18), self.taker)
        self.assertEqual(len(self.trades(receipt)), 2)
        self.assertEqual(self.bal(self.B, self.taker), 10_792 * E18)
        self.assertEqual(self.bal(self.B, self.fee_recipient), 8 * E18)
        self.assertEqual(self.bal(self.B, self.maker), 9_599 * E18 + E18 // 2)
        self.assertEqual(self.bal(self.B, self.third), 9_600 * E18 + E18 // 2)
        self.assertEqual(self.bal(self.B, self.book.address), 0)

    def test_expiry_and_self_trade_do_not_charge_fee(self):
        self.place(self.maker, True, duration=600)
        self.assertEqual(self.bal(self.B, self.book.address), 808 * E18)
        self.assertEqual(self.trades(self.place(self.maker, False)), [])
        self.assertEqual(self.bal(self.A, self.maker), 10 * E18)
        self.warp(601)
        self.send(self.book.functions.refundExpired(1), self.taker)
        self.assertEqual(self.bal(self.B, self.maker), 10_000 * E18)
        self.assertEqual(self.bal(self.B, self.fee_recipient), 0)

    def test_blocked_fee_recipient_does_not_block_market_and_fee_stays_backed(self):
        self.place(self.maker, False)
        self.B.functions.setBlocked(self.fee_recipient, True).transact()
        take = self.market(True, 800 * UNIT, 500_000, 404 * E18)
        self.send(take, self.taker)
        self.assertEqual(self.bal(self.B, self.maker), 10_400 * E18)
        self.assertEqual(self.bal(self.A, self.taker), 10 * E18 + E18 // 2)
        credit = self.book.functions.claimable(self.B.address, self.fee_recipient)
        self.assertEqual(credit.call(), 4 * E18)
        self.assertEqual(self.bal(self.B, self.book.address), 4 * E18)
        self.B.functions.setBlocked(self.fee_recipient, False).transact()
        self.send(self.book.functions.withdraw(self.B.address, self.fee_recipient), self.third)
        self.assertEqual(credit.call(), 0)
        self.assertEqual(self.bal(self.B, self.fee_recipient), 4 * E18)
        self.assertEqual(self.bal(self.B, self.third), 10_000 * E18)

    def test_market_buy_checks_remaining_bound_before_transferring(self):
        self.place(self.maker, False, 800)
        self.A.functions.setBlocked(self.taker, True).transact()
        take = self.market(True, 800 * UNIT, UNIT, 799 * E18)
        self.expect_revert("PriceChanged", take, **{"from": self.taker})
        tx = take.transact({"from": self.taker, "gas": 1_000_000})
        self.assertEqual(self.w3.eth.wait_for_transaction_receipt(tx).status, 0)
        self.assertEqual(self.book.functions.orderCount().call(), 1)
        self.assertEqual(self.bal(self.A, self.book.address), E18)
        self.assertEqual(self.bal(self.B, self.taker), 10_000 * E18)
        self.assertEqual(self.bal(self.B, self.fee_recipient), 0)


class TestFeeChanges(OrderBookFixture):
    with_fee = True

    def schedule(self, rate):
        return self.authority_call(self.book.functions.scheduleFeeChange(rate))

    def activate(self, rate):
        self.schedule(rate)
        self.warp(3600)
        self.send(self.book.functions.executeFeeChange(), self.third)

    def test_only_contract_authority_can_schedule_with_cap_delay_and_cancellation(self):
        self.assertEqual(self.book.functions.MAX_FEE_BPS().call(), 200)
        self.assertEqual(self.book.functions.FEE_CHANGE_DELAY().call(), 3600)
        self.expect_revert("OnlyFeeAuthority", self.book.functions.scheduleFeeChange(150), **{"from": self.deployer})
        self.expect_revert("FeeTooHigh", self.authority_function(self.book.functions.scheduleFeeChange(201)),
                           **{"from": self.deployer})
        receipt = self.schedule(150)
        scheduled, = self.book.events.FeeChangeScheduled().process_receipt(receipt, errors=DISCARD)
        self.assertEqual(scheduled["args"]["newBps"], 150)
        self.assertEqual(scheduled["args"]["executeAfter"], self.now() + 3600)
        execute = self.book.functions.executeFeeChange()
        self.expect_revert("FeeChangeNotReady", execute, **{"from": self.third})
        self.expect_revert("FeeChangePending", self.authority_function(self.book.functions.scheduleFeeChange(200)),
                           **{"from": self.deployer})
        cancel = self.book.functions.cancelFeeChange()
        self.expect_revert("OnlyFeeAuthority", cancel, **{"from": self.taker})
        self.authority_call(cancel)
        self.warp(3600)
        self.expect_revert("NoFeeChange", execute, **{"from": self.third})
        self.assertEqual(self.book.functions.activeFeeBps().call(), 100)
        self.activate(200)
        self.assertEqual(self.book.functions.activeFeeBps().call(), 200)
        self.expect_revert("NoFeeChange", execute, **{"from": self.third})

    def test_old_bid_uses_its_own_reserve_after_rate_increases(self):
        self.place(self.maker, True, duration=2 * DAY)
        self.schedule(200)
        self.assertEqual(self.book.functions.activeFeeBps().call(), 100)
        self.warp(3600)
        self.send(self.book.functions.executeFeeChange(), self.third)
        self.assertEqual(self.book.functions.getOrder(1).call()[-1], 100)
        fill, = self.trades(self.place(self.taker, False, lots=500_000))
        self.assertEqual(fill["fee"], 8 * E18)
        self.assertEqual(self.bal(self.B, self.taker), 10_392 * E18)
        self.assertEqual(self.bal(self.B, self.book.address), 404 * E18)
        self.warp(2 * DAY)
        self.send(self.book.functions.refundExpired(1), self.third)
        self.assertEqual(self.bal(self.B, self.maker), 9_600 * E18)
        self.assertEqual(self.bal(self.B, self.book.address), 0)

    def test_bounded_continuation_keeps_newer_orders_original_rate(self):
        for i in range(18):
            self.place(self.maker, False, lots=10_000, after=i)
        first = self.trades(self.place(self.taker, True, lots=180_000))
        self.assertEqual(len(first), 16)
        self.activate(0)
        resumed = self.trades(self.send(self.book.functions.advance(), self.third))
        self.assertEqual([e["fee"] for e in resumed], [8 * E18 // 100] * 2)
        self.assertEqual(self.bal(self.B, self.fee_recipient), 144 * E18 // 100)
        self.assertEqual(self.bal(self.B, self.book.address), 0)

    def test_stale_consent_fails_before_deposit_for_limit_and_market(self):
        self.place(self.maker, False)
        self.activate(200)
        for call in (
            self.book.functions.placeLimit(True, 800 * UNIT, UNIT, DAY, 0, 100),
            self.book.functions.placeLimit(False, 801 * UNIT, UNIT, DAY, 1, 100),
            self.book.functions.takeMarket(True, 800 * UNIT, UNIT, 900 * E18, 100),
        ):
            self.expect_revert("FeeChanged", call, **{"from": self.taker})
        self.assertEqual(self.book.functions.orderCount().call(), 1)
        self.assertEqual(self.bal(self.B, self.taker), 10_000 * E18)
        self.assertEqual(self.bal(self.A, self.book.address), E18)


class TestFixedFee(OrderBookFixture):
    with_fee = True
    fixed_fee = True

    def test_fee_cannot_be_changed_by_any_account_or_zero_sender_call(self):
        self.assertEqual(self.book.functions.FEE_AUTHORITY().call(), ZERO)
        self.assertEqual(self.book.functions.MAX_FEE_BPS().call(), 100)
        self.assertEqual(self.book.functions.FEE_CHANGE_DELAY().call(), 0)
        for who in (self.deployer, self.maker, self.fee_recipient):
            self.expect_revert("OnlyFeeAuthority", self.book.functions.scheduleFeeChange(0), **{"from": who})
            self.expect_revert("OnlyFeeAuthority", self.book.functions.cancelFeeChange(), **{"from": who})
        # A funded zero sender is possible in eth_call, even though it cannot sign a transaction.
        self.w3.eth.send_transaction({"from": self.deployer, "to": ZERO, "value": E18})
        selector = Web3.keccak(text="OnlyFeeAuthority()")[:4]
        for function in (self.book.functions.scheduleFeeChange(0), self.book.functions.cancelFeeChange()):
            with self.assertRaises(Exception) as cm:
                function.call({"from": ZERO})
            match = re.search(r"reverted: (b['\"].*)$", str(cm.exception), re.S)
            self.assertIsNotNone(match, str(cm.exception))
            self.assertEqual(ast.literal_eval(match.group(1))[:4], selector)
        self.warp(DAY)
        self.expect_revert("NoFeeChange", self.book.functions.executeFeeChange(), **{"from": self.third})
        self.assertEqual(self.book.functions.activeFeeBps().call(), 100)
        self.assertEqual(self.book.functions.feeChangeAfter().call(), 0)

    def test_partial_bid_settlement_cancel_and_expiry_refund_keep_one_percent(self):
        self.place(self.maker, True)
        fill, = self.trades(self.place(self.taker, False, lots=UNIT // 2))
        self.assertEqual(fill["fee"], 4 * E18)
        self.assertEqual(self.bal(self.B, self.taker), 10_396 * E18)
        self.assertEqual(self.bal(self.B, self.book.address), 404 * E18)
        self.send(self.book.functions.cancel(1), self.maker)
        self.assertEqual(self.bal(self.B, self.maker), 9_600 * E18)
        self.assertEqual(self.bal(self.B, self.book.address), 0)
        self.place(self.third, True, duration=600)
        fill, = self.trades(self.place(self.fourth, False, lots=UNIT // 4))
        self.assertEqual(fill["fee"], 2 * E18)
        self.warp(601)
        self.send(self.book.functions.refundExpired(3), self.maker)
        self.assertEqual(self.bal(self.B, self.third), 9_800 * E18)
        self.assertEqual(self.bal(self.B, self.fee_recipient), 6 * E18)
        self.assertEqual(self.bal(self.B, self.book.address), 0)

    def test_zero_authority_requires_equal_initial_and_max_and_zero_delay(self):
        art = ART['P2POrderBook']
        factory = self.w3.eth.contract(abi=art['abi'], bytecode=art['bin'])
        for initial, maximum, delay in ((100, 200, 0), (100, 100, 1)):
            self.expect_revert("BadFeeConfig", factory.constructor(
                self.A.address, self.B.address, self.fee_recipient, ZERO, initial, maximum, delay),
                **{"from": self.deployer})
