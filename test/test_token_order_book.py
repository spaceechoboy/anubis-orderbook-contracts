"""New-book accounting is exercised identically for nine and eighteen decimals."""
from web3 import EthereumTesterProvider, Web3
from web3.logs import DISCARD
from test_escrow import ART, Base, DAY, E18

UNIT = 10**6
MAX64 = 2**64 - 1
ZERO = '0x' + '0' * 40


class TokenBookCases:
    decimals = 9

    def setUp(self):
        self.assertTrue('P2PTokenOrderBook' in ART, 'new fixed-fee multi-decimal contract is not built')
        self.w3 = Web3(EthereumTesterProvider())
        self.t = self.w3.provider.ethereum_tester
        self.deployer, self.maker, self.taker, self.third, self.fourth, self.fee_recipient = self.w3.eth.accounts[:6]
        self.unit = 10**self.decimals
        self.lot = self.unit // UNIT
        self.A = self.deploy('TokenBookMock', self.decimals)
        self.B = self.deploy('TokenBookMock', 18)
        self.book = self.deploy('P2PTokenOrderBook', self.A.address, self.B.address, self.fee_recipient)
        for who in (self.maker, self.taker, self.third, self.fourth):
            self.A.functions.mint(who, 100 * self.unit).transact()
            self.B.functions.mint(who, 10_000 * E18).transact()
            self.A.functions.approve(self.book.address, 2**256-1).transact({'from': who})
            self.B.functions.approve(self.book.address, 2**256-1).transact({'from': who})

    def send(self, function, owner):
        receipt = self.w3.eth.wait_for_transaction_receipt(function.transact({'from': owner}))
        self.assertEqual(receipt.status, 1)
        return receipt

    def place(self, who, buy, price=800*UNIT, lots=UNIT, after=0, duration=DAY):
        return self.send(self.book.functions.placeLimit(buy, price, lots, duration, after, 100), who)

    def take(self, who, buy, price, lots, bound):
        return self.send(self.book.functions.takeMarket(buy, price, lots, bound, 100), who)

    def trades(self, receipt):
        return [e['args'] for e in self.book.events.Trade().process_receipt(receipt, errors=DISCARD)]

    def assert_backed(self):
        """Recompute every open reserve and every tested account credit independently."""
        liabilities = {self.A.address: 0, self.B.address: 0}
        for i in range(1, self.book.functions.orderCount().call()+1):
            maker, expiry, buy, status, price, lots, filled, fee = self.book.functions.getOrder(i).call()
            if status == 0:
                left = lots-filled
                if buy:
                    gross = price*left*10**6
                    liabilities[self.B.address] += gross + gross//100
                else:
                    liabilities[self.A.address] += left*self.lot
        for token in (self.A, self.B):
            for who in self.w3.eth.accounts:
                liabilities[token.address] += self.book.functions.claimable(token.address, who).call()
            self.assertEqual(self.bal(token, self.book.address), liabilities[token.address])

    def test_metadata_fixed_fee_and_smallest_lot_exact_quote(self):
        self.assertEqual(self.book.functions.LOT_SIZE().call(), self.lot)
        self.assertEqual(self.book.functions.PRICE_TICK().call(), 10**12)
        self.assertEqual(self.book.functions.BASE_DECIMALS().call(), self.decimals)
        self.assertEqual(self.book.functions.activeFeeBps().call(), 100)
        self.assertEqual(self.book.functions.FEE_AUTHORITY().call(), ZERO)
        for fn in (self.book.functions.scheduleFeeChange(0), self.book.functions.cancelFeeChange()):
            self.expect_revert('OnlyFeeAuthority', fn, **{'from': self.fee_recipient})
        self.expect_revert('NoFeeChange', self.book.functions.executeFeeChange(), **{'from': self.third})
        self.place(self.maker, False, price=1, lots=1)
        fill, = self.trades(self.place(self.taker, True, price=1, lots=1))
        self.assertEqual((fill['quote'], fill['fee']), (10**6, 10**4))
        self.assertEqual(self.bal(self.A, self.taker), 100*self.unit + self.lot)
        self.assertEqual(self.bal(self.B, self.fee_recipient), 10**4)
        self.assert_backed()

    def test_partial_bid_cancel_and_expiry_refunds(self):
        self.place(self.maker, True)
        self.assertEqual(self.bal(self.B, self.book.address), 808*E18)
        self.place(self.taker, False, price=790*UNIT, lots=UNIT//2)
        self.assertEqual(self.bal(self.B, self.taker), 10_396*E18)
        self.assertEqual(self.bal(self.B, self.maker), 9_196*E18)
        self.assert_backed()
        self.send(self.book.functions.cancel(1), self.maker)
        self.assertEqual(self.bal(self.B, self.maker), 9_600*E18)
        self.place(self.third, True, duration=600)
        self.place(self.fourth, False, lots=UNIT//4)
        self.warp(601)
        self.send(self.book.functions.refundExpired(3), self.maker)
        self.assertEqual(self.bal(self.B, self.third), 9_800*E18)
        self.assertEqual(self.bal(self.B, self.fee_recipient), 6*E18)
        self.assert_backed()

    def test_split_fills_equal_single_fill_and_receipt_total(self):
        def scenario(parts):
            self.place(self.maker, False, price=1_234_567, lots=17)
            for part in parts:
                self.place(self.taker, True, price=1_234_567, lots=part)
                self.assert_backed()
            return ([self.bal(token, who) for token in (self.A, self.B)
                     for who in (self.maker, self.taker, self.fee_recipient, self.book.address)],
                    self.book.functions.getOrderReceipt(1).call()[1:3])
        snapshot = self.t.take_snapshot()
        once = scenario([17])
        self.t.revert_to_snapshot(snapshot)
        split = scenario([1, 3, 7, 6])
        self.assertEqual(once, split)
        self.assertEqual(split[1], [1_234_567*17*10**6, 0])

    def test_price_priority_fifo_and_receipts(self):
        self.place(self.maker, False, price=801*UNIT, lots=UNIT//2)
        self.place(self.third, False, price=799*UNIT, lots=UNIT//4)
        self.expect_revert('BadPosition', self.book.functions.placeLimit(False, 799*UNIT, UNIT//4, DAY, 0, 100), **{'from': self.fourth})
        self.place(self.fourth, False, price=799*UNIT, lots=UNIT//4, after=2)
        fills = self.trades(self.place(self.taker, True, price=805*UNIT, lots=3*UNIT//4))
        self.assertEqual([(f['askId'], f['price']) for f in fills], [(2,799*UNIT),(3,799*UNIT),(1,801*UNIT)])
        gross = 599_750*10**15
        self.assertEqual(self.bal(self.B, self.taker), 10_000*E18-gross-gross//100)
        self.assertEqual(self.book.functions.getOrderReceipt(4).call()[1:3], [gross,gross//100])
        self.assert_backed()

    def test_market_bounds_revert_entire_book_then_exact_fill(self):
        self.place(self.maker, False, price=799*UNIT, lots=UNIT//2)
        self.place(self.third, False, price=801*UNIT, lots=UNIT//2, after=1)
        fn = self.book.functions.takeMarket(True, 801*UNIT, UNIT, 808*E18-1, 100)
        self.expect_revert('PriceChanged', fn, **{'from': self.taker})
        failed = self.w3.eth.wait_for_transaction_receipt(fn.transact({'from':self.taker,'gas':2_000_000}))
        self.assertEqual((failed.status,len(failed.logs)),(0,0))
        self.assertEqual(self.book.functions.orderCount().call(), 2)
        self.assert_backed()
        self.take(self.taker, True, 801*UNIT, UNIT, 808*E18)
        self.assertEqual(self.bal(self.A,self.taker),101*self.unit)
        self.assertEqual(self.bal(self.B,self.taker),9_192*E18)
        self.assert_backed()

    def test_market_sell_net_floor_and_maker_reserve_release(self):
        self.place(self.maker, True, price=801*UNIT, lots=UNIT//2)
        self.place(self.third, True, price=799*UNIT, lots=UNIT//2, after=1)
        self.expect_revert('PriceChanged',self.book.functions.takeMarket(False,799*UNIT,UNIT,792*E18+1,100),**{'from':self.taker})
        self.assert_backed()
        self.take(self.taker,False,799*UNIT,UNIT,792*E18)
        self.assertEqual(self.bal(self.B,self.taker),10_792*E18)
        self.assertEqual(self.bal(self.B,self.fee_recipient),8*E18)
        self.assert_backed()

    def test_sixteen_step_market_atomicity_and_limit_continuation(self):
        for i in range(18):
            self.place(self.maker,False,lots=1,after=i)
        self.expect_revert('NotEnoughLiquidity',self.book.functions.takeMarket(True,800*UNIT,18,10**18,100),**{'from':self.taker})
        self.assertEqual(self.book.functions.orderCount().call(),18)
        self.assert_backed()
        fills = self.trades(self.place(self.taker,True,lots=18))
        self.assertEqual([f['askId'] for f in fills],list(range(1,17)))
        self.assertTrue(self.book.functions.needsAdvance().call())
        self.assert_backed()
        fills = self.trades(self.send(self.book.functions.advance(),self.third))
        self.assertEqual([f['askId'] for f in fills],[17,18])
        self.assertFalse(self.book.functions.needsAdvance().call())
        self.assert_backed()

    def test_market_has_separate_sixteen_step_advance_and_fill_budgets(self):
        # A limit placement fills sixteen, leaving sixteen crossed pairs for advance.
        for i in range(48):
            self.place(self.maker,False,price=(800 if i<32 else 801)*UNIT,lots=1,after=i)
        self.assertEqual(len(self.trades(self.place(self.taker,True,lots=32))),16)
        self.assertTrue(self.book.functions.needsAdvance().call())
        # One call resumes sixteen old matches, then fills sixteen new market lots.
        receipt=self.take(self.third,True,801*UNIT,16,10**18)
        fills=self.trades(receipt)
        self.assertEqual([f['askId'] for f in fills],list(range(17,49)))
        self.assertEqual([f['bidId'] for f in fills],[49]*16+[50]*16)
        self.assert_backed()

    def test_blocked_payout_cancel_expiry_and_permissionless_fixed_recipient_claim(self):
        self.place(self.maker,False)
        self.A.functions.setBlocked(self.maker,True).transact()
        receipt = self.send(self.book.functions.cancel(1),self.maker)
        self.assertEqual(len(self.book.events.PayoutDeferred().process_receipt(receipt,errors=DISCARD)),1)
        self.assertEqual(self.book.functions.claimable(self.A.address,self.maker).call(),self.unit)
        self.assert_backed()
        self.expect_revert('TransferFailed',self.book.functions.withdraw(self.A.address,self.maker),**{'from':self.third})
        self.A.functions.setBlocked(self.maker,False).transact()
        self.send(self.book.functions.withdraw(self.A.address,self.maker),self.third)
        self.assertEqual(self.bal(self.A,self.third),100*self.unit)
        self.assertEqual(self.bal(self.A,self.maker),100*self.unit)
        self.place(self.maker,True,duration=600)
        self.B.functions.setBlocked(self.maker,True).transact()
        self.warp(601)
        self.send(self.book.functions.refundExpired(2),self.third)
        self.assertEqual(self.book.functions.claimable(self.B.address,self.maker).call(),808*E18)
        self.assert_backed()

    def test_blocked_fee_recipient_retains_fully_backed_claim(self):
        self.place(self.maker,False)
        self.B.functions.setBlocked(self.fee_recipient,True).transact()
        self.take(self.taker,True,800*UNIT,UNIT,808*E18)
        self.assertEqual(self.book.functions.claimable(self.B.address,self.fee_recipient).call(),8*E18)
        self.assert_backed()
        self.B.functions.setBlocked(self.fee_recipient,False).transact()
        self.send(self.book.functions.withdraw(self.B.address,self.fee_recipient),self.third)
        self.assertEqual(self.bal(self.B,self.fee_recipient),8*E18)
        self.assert_backed()

    def test_sender_and_receiver_tax_deposits_revert_with_no_order_or_balance_change(self):
        for token,buy in ((self.A,False),(self.B,True)):
            for sender_tax in (True,False):
                with self.subTest(token=token.address,sender_tax=sender_tax):
                    token.functions.setTax(100 if sender_tax else 0,0 if sender_tax else 100).transact()
                    before = self.bal(token,self.maker)
                    self.expect_revert('TransferMismatch',self.book.functions.placeLimit(buy,800*UNIT,UNIT,DAY,0,100),**{'from':self.maker})
                    self.assertEqual(self.bal(token,self.maker),before)
                    self.assertEqual(self.book.functions.orderCount().call(),0)
                    self.assert_backed()
                    token.functions.setTax(0,0).transact()

    def test_sender_and_receiver_tax_payouts_cannot_spend_other_order_collateral(self):
        for sender_tax in (True,False):
            with self.subTest(sender_tax=sender_tax):
                snapshot=self.t.take_snapshot()
                self.place(self.maker,False)
                self.place(self.third,False,price=801*UNIT,after=1)
                self.A.functions.setTax(100 if sender_tax else 0,0 if sender_tax else 100).transact()
                self.place(self.taker,True)
                self.assertEqual(self.bal(self.A,self.book.address),2*self.unit)
                self.assertEqual(self.book.functions.claimable(self.A.address,self.taker).call(),self.unit)
                self.assert_backed()
                self.A.functions.setTax(0,0).transact()
                self.send(self.book.functions.withdraw(self.A.address,self.taker),self.third)
                self.assertEqual(self.bal(self.A,self.book.address),self.unit)
                self.assert_backed()
                self.t.revert_to_snapshot(snapshot)

    def test_quote_tax_after_bid_deposit_defers_all_exact_liabilities(self):
        for sender_tax in (True,False):
            with self.subTest(sender_tax=sender_tax):
                snapshot=self.t.take_snapshot()
                self.place(self.maker,True)
                self.B.functions.setTax(100 if sender_tax else 0,0 if sender_tax else 100).transact()
                self.place(self.taker,False)
                for who,amount in ((self.maker,8*E18),(self.taker,792*E18),(self.fee_recipient,8*E18)):
                    self.assertEqual(self.book.functions.claimable(self.B.address,who).call(),amount)
                self.assert_backed()
                self.B.functions.setTax(0,0).transact()
                for who in (self.maker,self.taker,self.fee_recipient):
                    self.send(self.book.functions.withdraw(self.B.address,who),self.third)
                self.assertEqual(self.bal(self.B,self.maker),9_200*E18)
                self.assertEqual(self.bal(self.B,self.taker),10_792*E18)
                self.assertEqual(self.bal(self.B,self.third),10_000*E18)
                self.assert_backed()
                self.t.revert_to_snapshot(snapshot)

    def test_stale_fee_consent_and_external_deliver_cannot_move_escrow(self):
        self.place(self.maker,False)
        for fn in (self.book.functions.placeLimit(True,800*UNIT,UNIT,DAY,0,0),
                   self.book.functions.takeMarket(True,800*UNIT,UNIT,808*E18,0)):
            self.expect_revert('FeeChanged',fn,**{'from':self.taker})
        self.expect_revert('OnlySelf',self.book.functions.deliver(self.A.address,self.third,self.unit),**{'from':self.third})
        self.assertEqual(self.book.functions.orderCount().call(),1)
        self.assert_backed()

    def test_market_blocked_recipient_reverts_without_claim(self):
        self.place(self.maker,False)
        self.A.functions.setBlocked(self.taker,True).transact()
        self.expect_revert('TransferFailed',self.book.functions.takeMarket(True,800*UNIT,UNIT,808*E18,100),**{'from':self.taker})
        self.assertEqual(self.book.functions.orderCount().call(),1)
        self.assertEqual(self.book.functions.claimable(self.A.address,self.taker).call(),0)
        self.assert_backed()

    def test_reentrancy_is_rejected_during_deposit_and_payout(self):
        data = self.book.encode_abi('advance')
        self.A.functions.arm(self.book.address,bytes.fromhex(data[2:])).transact()
        self.place(self.maker,False)
        self.assertTrue(self.A.functions.fired().call())
        self.assertFalse(self.A.functions.reentrySucceeded().call())
        self.assertEqual(self.A.functions.lastErr().call(),Web3.keccak(text='Reentrancy()')[:4])
        self.A.functions.arm(self.book.address,bytes.fromhex(data[2:])).transact()
        self.place(self.taker,True)
        self.assertTrue(self.A.functions.fired().call())
        self.assertFalse(self.A.functions.reentrySucceeded().call())
        self.assertEqual(self.A.functions.lastErr().call(),Web3.keccak(text='Reentrancy()')[:4])
        self.assert_backed()

    def test_uint64_max_product_reserves_and_full_fill_are_exact(self):
        base=MAX64*self.lot
        gross=MAX64*MAX64*10**6
        reserve=gross+gross//100
        self.A.functions.mint(self.maker,base).transact()
        self.B.functions.mint(self.taker,reserve).transact()
        self.place(self.maker,False,price=MAX64,lots=MAX64)
        fill,=self.trades(self.place(self.taker,True,price=MAX64,lots=MAX64))
        self.assertEqual((fill['quote'],fill['fee']),(gross,gross//100))
        self.assertEqual(self.book.functions.getOrderReceipt(2).call()[1:3],[gross,gross//100])
        self.assert_backed()

    def test_expiry_pruning_keeps_refund_liability_and_self_trade_charges_nothing(self):
        self.place(self.maker,False,duration=600)
        self.place(self.maker,True)
        self.assertEqual(self.book.functions.getOrder(2).call()[3],4)
        self.assertEqual(self.bal(self.B,self.fee_recipient),0)
        self.warp(601)
        self.send(self.book.functions.advance(),self.third)
        self.assertEqual(self.book.functions.getBook(False,0,64).call()[0],[])
        self.assert_backed()
        self.send(self.book.functions.refundExpired(1),self.third)
        self.assert_backed()

    def test_constructor_rejects_unsupported_decimals_and_invalid_recipient(self):
        art=ART['P2PTokenOrderBook']
        factory=self.w3.eth.contract(abi=art['abi'],bytecode=art['bin'])
        for decimals in (0,6,8,10,19):
            wrong=self.deploy('TokenBookMock',decimals)
            self.expect_revert('BadTokens',factory.constructor(wrong.address,self.B.address,self.fee_recipient),**{'from':self.deployer})
        quote9=self.deploy('TokenBookMock',9)
        self.expect_revert('BadTokens',factory.constructor(self.A.address,quote9.address,self.fee_recipient),**{'from':self.deployer})
        self.expect_revert('BadTokens',factory.constructor(self.A.address,self.A.address,self.fee_recipient),**{'from':self.deployer})
        self.expect_revert('BadFeeRecipient',factory.constructor(self.A.address,self.B.address,ZERO),**{'from':self.deployer})


class TestTokenBook9(TokenBookCases,Base):
    decimals=9


class TestTokenBook18(TokenBookCases,Base):
    decimals=18
