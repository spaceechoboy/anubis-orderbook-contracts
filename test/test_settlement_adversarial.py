"""Independent acceptance tests against the deployed local EVM settlement artifact.

These tests deliberately exercise valid gate-key misuse, cancellation ordering,
late cross-asset callbacks, and an independent stateful wallet ledger.
"""
import random
from test_noncustodial_settlement import SettlementBase, ORDER_FIELDS, E18, ZERO


class TestSettlementAdversarial(SettlementBase):
    def test_fee_recipient_alias_partial_fills_match_cumulative_wallet_ledger(self):
        # Fable R2: the original four alias cases covered only full fills.
        for buy in (False, True):
            for recipient in (self.maker, self.taker):
                with self.subTest(buy=buy, recipient=recipient):
                    market = self.new_market(fee=recipient)
                    self.seed(market)
                    order = self.order(market, buy=buy, totalLots=100,
                                       priceTicks=1_234_567)
                    initial = {who: (self.bal(self.A, who), self.bal(self.B, who))
                               for who in (self.maker, self.taker)}
                    filled = 0
                    for amount in (1, 39, 60):
                        self.send(self.call_fill(order, self.permit(order, amount, market), market))
                        filled += amount
                        gross = filled * 1_234_567 * 1_000_000
                        fee = gross // 100
                        base = filled * 1_000_000_000_000
                        # Aggregate independent economic flows by unique wallet.
                        delta = {self.maker: [base if buy else -base, -gross if buy else gross],
                                 self.taker: [-base if buy else base, gross-fee if buy else -gross-fee]}
                        delta[recipient][1] += fee
                        for who, (da, db) in delta.items():
                            self.assertEqual(self.bal(self.A, who) - initial[who][0], da)
                            self.assertEqual(self.bal(self.B, who) - initial[who][1], db)
                        self.assertEqual(market.functions.filledLots(self.digest('Order', order, market)).call(), filled)
                        self.assertEqual((self.bal(self.A, market.address), self.bal(self.B, market.address)), (0, 0))
                    self.atomic_revert(order, self.permit(order, 1, market), market)

    def test_valid_gate_cannot_replace_maker_terms_or_overfill(self):
        original = self.order()
        signature = self.signed('Order', original, self.maker)
        for changes in ({'priceTicks': 1}, {'totalLots': 200_000_000},
                        {'maker': self.third}, {'feeRecipient': self.third},
                        {'expiresAt': original['expiresAt'] + 1}):
            changed = {**original, **changes}
            permit = self.permit(changed)
            self.atomic_revert(changed, permit,
                               fn=self.call_fill(changed, permit, msig=signature))
        self.atomic_revert(original, self.permit(original, 100_000_001))
        # A compromised gate CAN select the first counterparty, within signed terms.
        first = self.permit(original, 40_000_000, taker=self.third)
        self.send(self.call_fill(original, first), self.third)
        self.assertEqual(self.market.functions.boundTaker(first['orderHash']).call(), self.third)
        self.atomic_revert(original, self.permit(original, 60_000_000))
        final = self.permit(original, 60_000_000, taker=self.third)
        self.send(self.call_fill(original, final), self.third)
        self.assertEqual(self.bal(self.B, self.market.address), 0)

    def test_identical_market_another_deployment_cannot_replay_order_signature(self):
        original = self.order()
        signature = self.signed('Order', original, self.maker)
        other = self.new_market()
        self.seed(other)
        # Same market, nonce and deployment-config digest; only domain address differs.
        permit = self.permit(original, market=other)
        self.atomic_revert(original, permit, other,
                           fn=self.call_fill(original, permit, other, msig=signature))
        self.send(self.call_fill(original, permit, other))
        self.assertEqual(self.market.functions.filledLots(self.digest('Order', original)).call(), 0)

    def test_cancel_before_fill_and_fill_before_cancel_both_conserve_wallet_assets(self):
        before = self.order()
        permit = self.permit(before)
        self.send(self.market.functions.cancelNonce(before['makerNonce']), self.maker)
        self.atomic_revert(before, permit)
        after = self.order()
        self.send(self.call_fill(after, self.permit(after, 40_000_000)))
        remainder = self.permit(after, 60_000_000)
        self.send(self.market.functions.cancelAll(1), self.maker)
        self.atomic_revert(after, remainder)
        self.assertEqual(self.market.functions.filledLots(remainder['orderHash']).call(), 40_000_000)
        self.assertEqual((self.bal(self.A, self.market.address), self.bal(self.B, self.market.address)), (0, 0))
        # Neither a new gate permit nor a later epoch can resurrect the old signed order.
        self.send(self.market.functions.cancelAll(2), self.maker)
        self.atomic_revert(after, self.permit(after, 60_000_000))

    def test_expiry_at_exact_latest_block_rejects_eth_call_without_refund(self):
        order = self.order(expiresAt=self.now() + 40)
        permit = self.permit(order)
        self.t.time_travel(order['expiresAt'])
        self.t.mine_block()
        self.assertEqual(self.now(), order['expiresAt'])
        state = self.snapshot(order, permit)
        with self.assertRaises(Exception):
            self.call_fill(order, permit).call({'from': self.taker}, block_identifier='latest')
        self.assertEqual(self.snapshot(order, permit), state)

    def test_fee_leg_failure_restores_base_and_quote_finite_allowances(self):
        self.A.functions.approve(self.market.address, 100 * E18).transact({'from': self.maker})
        self.B.functions.approve(self.market.address, 202 * E18).transact({'from': self.taker})
        self.B.functions.setBlocked(self.feeRcv, True).transact()
        order = self.order()
        permit = self.permit(order)
        self.atomic_revert(order, permit)
        self.assertEqual(self.A.functions.allowance(self.maker, self.market.address).call(), 100 * E18)
        self.assertEqual(self.B.functions.allowance(self.taker, self.market.address).call(), 202 * E18)
        self.B.functions.setBlocked(self.feeRcv, False).transact()
        self.send(self.call_fill(order, permit))
        self.assertEqual(self.A.functions.allowance(self.maker, self.market.address).call(), 0)
        self.assertEqual(self.B.functions.allowance(self.taker, self.market.address).call(), 0)

    def test_balance_change_is_execution_failure_and_does_not_consume_permit(self):
        order = self.order()
        permit = self.permit(order)
        balance = self.bal(self.A, self.maker)
        self.A.functions.transfer(self.third, balance).transact({'from': self.maker})
        self.atomic_revert(order, permit)
        self.A.functions.transfer(self.maker, balance).transact({'from': self.third})
        self.send(self.call_fill(order, permit))

    def test_late_nft_callback_cannot_change_dai_or_deposit_it_into_settlement(self):
        market = self.new_market(self.nft, 0, 1)
        self.nft.functions.approve(market.address, 1).transact({'from': self.maker})
        receiver = self.deploy('NftMarketReceiver')
        self.B.functions.mint(receiver.address, 1_000 * E18).transact()
        receiver.functions.execute(self.B.address, bytes.fromhex(
            self.B.functions.approve(market.address, 101 * E18)._encode_transaction_data()[2:])).transact()
        for target in (receiver.address, market.address, self.maker, self.feeRcv):
            callback = self.B.functions.mint(target, 1)._encode_transaction_data()
            receiver.functions.configure(False, self.B.address, bytes.fromhex(callback[2:]), ZERO).transact()
            order = self.order(market, nftMode=1, tokenId=1, totalLots=1,
                               minFillLots=1, allowPartial=False, priceTicks=100_000_000)
            permit = self.permit(order, 1, market, taker=receiver.address)
            fill = self.call_fill(order, permit, market)
            wrapper = receiver.functions.execute(market.address, bytes.fromhex(fill._encode_transaction_data()[2:]))
            self.atomic_revert(order, permit, market, self.nft, fn=wrapper, who=self.deployer)
            self.assertEqual(self.nft.functions.ownerOf(1).call(), self.maker)
            self.assertEqual(self.nft.functions.getApproved(1).call(), market.address)
            self.assertEqual(self.B.functions.allowance(receiver.address, market.address).call(), 101 * E18)

    def test_seeded_partial_fill_ledger_matches_exact_wallet_net_flows(self):
        rng = random.Random(6714)
        for buy in (False, True):
            initial = {who: (self.bal(self.A, who), self.bal(self.B, who))
                       for who in (self.maker, self.taker, self.feeRcv)}
            order = self.order(buy=buy, totalLots=1_000_000, priceTicks=1_234_567)
            filled = 0
            for _ in range(24):
                amount = rng.randrange(1, 20_000)
                self.send(self.call_fill(order, self.permit(order, amount)))
                filled += amount
                gross = filled * 1_234_567 * 1_000_000
                fee = filled * 1_234_567 * 10_000
                token = filled * 1_000_000_000_000
                expected = ({self.maker: (token, -gross), self.taker: (-token, gross-fee), self.feeRcv: (0, fee)}
                            if buy else
                            {self.maker: (-token, gross), self.taker: (token, -gross-fee), self.feeRcv: (0, fee)})
                for who, (da, db) in expected.items():
                    self.assertEqual(self.bal(self.A, who) - initial[who][0], da)
                    self.assertEqual(self.bal(self.B, who) - initial[who][1], db)
                self.assertEqual(self.market.functions.filledLots(self.digest('Order', order)).call(), filled)
                self.assertEqual((self.bal(self.A, self.market.address), self.bal(self.B, self.market.address)), (0, 0))
            self.send(self.market.functions.cancel(tuple(order[k] for k, _ in ORDER_FIELDS)), self.maker)
            self.atomic_revert(order, self.permit(order, order['totalLots'] - filled))

    def test_final_fee_callback_cannot_change_already_transferred_base_token(self):
        for target in (self.maker, self.taker, self.feeRcv, self.market.address):
            callback = self.A.functions.mint(target, 1)._encode_transaction_data()
            self.B.functions.setHook(self.A.address, bytes.fromhex(callback[2:]), 2).transact()
            order = self.order()
            self.atomic_revert(order)
            self.assertEqual(self.B.functions.calls().call(), 0)

    def test_final_fee_callback_cannot_move_received_nft_out_of_buyers_wallet(self):
        market = self.new_market(self.nft, 0, 1)
        self.nft.functions.approve(market.address, 1).transact({'from': self.maker})
        receiver = self.deploy('NftMarketReceiver')
        self.B.functions.mint(receiver.address, 1_000 * E18).transact()
        receiver.functions.execute(self.B.address, bytes.fromhex(
            self.B.functions.approve(market.address, 101 * E18)._encode_transaction_data()[2:])).transact()
        # The callback approves the quote token. Its second transfer later forwards the card.
        nft_approve = self.nft.functions.approve(self.B.address, 1)._encode_transaction_data()
        receiver.functions.configure(False, self.nft.address, bytes.fromhex(nft_approve[2:]), ZERO).transact()
        move = self.nft.functions.transferFrom(receiver.address, self.third, 1)._encode_transaction_data()
        self.B.functions.setHook(self.nft.address, bytes.fromhex(move[2:]), 2).transact()
        order = self.order(market, nftMode=1, tokenId=1, totalLots=1,
                           minFillLots=1, allowPartial=False, priceTicks=100_000_000)
        permit = self.permit(order, 1, market, taker=receiver.address)
        fill = self.call_fill(order, permit, market)
        wrapper = receiver.functions.execute(market.address, bytes.fromhex(fill._encode_transaction_data()[2:]))
        self.atomic_revert(order, permit, market, self.nft, fn=wrapper, who=self.deployer)
        self.assertEqual(self.nft.functions.ownerOf(1).call(), self.maker)
        self.assertEqual(self.nft.functions.getApproved(1).call(), market.address)

    def test_nft_transfer_and_approval_clear_are_reverted_when_last_fee_leg_fails(self):
        market = self.new_market(self.nft, 0, 1)
        self.nft.functions.approve(market.address, 1).transact({'from': self.maker})
        self.B.functions.approve(market.address, 101 * E18).transact({'from': self.taker})
        self.B.functions.setBlocked(self.feeRcv, True).transact()
        order = self.order(market, nftMode=1, tokenId=1, totalLots=1,
                           minFillLots=1, allowPartial=False, priceTicks=100_000_000)
        permit = self.permit(order, 1, market)
        self.atomic_revert(order, permit, market, self.nft)
        self.assertEqual(self.nft.functions.ownerOf(1).call(), self.maker)
        self.assertEqual(self.nft.functions.getApproved(1).call(), market.address)
        self.assertEqual(self.B.functions.allowance(self.taker, market.address).call(), 101 * E18)
        self.B.functions.setBlocked(self.feeRcv, False).transact()
        self.send(self.call_fill(order, permit, market))
        self.assertEqual(self.nft.functions.ownerOf(1).call(), self.taker)
        self.assertEqual(self.nft.functions.balanceOf(market.address).call(), 0)
        self.assertEqual(self.bal(self.B, market.address), 0)
