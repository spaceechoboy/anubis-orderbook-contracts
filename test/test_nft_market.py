"""Accounting and atomicity tests for the noncustodial Awake NFT market."""
from web3 import EthereumTesterProvider, Web3
from web3.logs import DISCARD
from test_escrow import ART, Base, DAY, E18

UNIT=10**6
ZERO='0x'+'0'*40


class TestNftMarket(Base):
    def setUp(self):
        self.assertTrue('AwakeNftMarket' in ART and 'NftMarketMock' in ART,'NFT market and mock must be built')
        self.w3=Web3(EthereumTesterProvider())
        self.t=self.w3.provider.ethereum_tester
        self.deployer,self.maker,self.taker,self.third,self.fourth,self.fee_recipient=self.w3.eth.accounts[:6]
        self.nft=self.deploy('NftMarketMock')
        self.B=self.deploy('TokenBookMock',18)
        self.market=self.deploy('AwakeNftMarket',self.nft.address,self.B.address,self.fee_recipient)
        self.parties=[self.maker,self.taker,self.third,self.fourth,self.fee_recipient]
        self.nft.functions.mintMany(self.maker,1,40).transact()
        self.nft.functions.mintMany(self.third,101,10).transact()
        for who in (self.maker,self.taker,self.third,self.fourth):
            self.B.functions.mint(who,10_000*E18).transact()
            self.B.functions.approve(self.market.address,2**256-1).transact({'from':who})
            self.nft.functions.setApprovalForAll(self.market.address,True).transact({'from':who})

    def send(self,fn,who):
        r=self.w3.eth.wait_for_transaction_receipt(fn.transact({'from':who}))
        self.assertEqual(r.status,1)
        return r

    def list_card(self,token=1,who=None,price=100*UNIT,duration=DAY):
        return self.send(self.market.functions.list(token,price,self.now()+duration),who or self.maker)

    def bid(self,cards=2,who=None,price=100*UNIT,duration=DAY):
        return self.send(self.market.functions.placeBid(price,cards,self.now()+duration),who or self.taker)

    def receiver(self,reject=False):
        receiver=self.deploy('NftMarketReceiver')
        self.parties.append(receiver.address)
        self.B.functions.mint(receiver.address,10_000*E18).transact()
        self.forward(receiver,self.B.functions.approve(self.market.address,2**256-1))
        receiver.functions.configure(reject,ZERO,b'',ZERO).transact()
        return receiver

    def forwarded(self,receiver,fn):
        return receiver.functions.execute(fn.address,bytes.fromhex(fn._encode_transaction_data()[2:]))

    def forward(self,receiver,fn):
        return self.send(self.forwarded(receiver,fn),self.deployer)

    def assert_backed(self):
        reserve=0
        for id in range(1,self.market.functions.bidCount().call()+1):
            _,price,_,_,remaining,status,_=self.market.functions.getBid(id).call()
            if status==0:
                gross=price*10**12*remaining
                reserve+=gross+gross//100
        claims=sum(self.market.functions.claimable(who).call() for who in self.parties)
        self.assertEqual(self.market.functions.reservedDAI().call(),reserve)
        self.assertEqual(self.market.functions.totalClaimable().call(),claims)
        self.assertEqual(self.bal(self.B,self.market.address),reserve+claims)

    def state(self,token=1):
        return (
            self.market.functions.listingCount().call(),self.market.functions.bidCount().call(),
            self.market.functions.tradeCount().call(),self.market.functions.mutationCount().call(),
            self.market.functions.reservedDAI().call(),self.market.functions.totalClaimable().call(),
            self.market.functions.activeListing(token).call(),self.nft.functions.ownerOf(token).call(),
            [self.bal(self.B,who) for who in self.parties+[self.market.address]],
            [self.market.functions.claimable(who).call() for who in self.parties],
            [self.market.functions.getListing(i).call() for i in range(1,self.market.functions.listingCount().call()+1)],
            [self.market.functions.getBid(i).call() for i in range(1,self.market.functions.bidCount().call()+1)],
        )

    def assert_atomic_revert(self,fn,who,token=1,error=None):
        before=self.state(token)
        if error:
            self.expect_revert(error,fn,**{'from':who})
        r=self.w3.eth.wait_for_transaction_receipt(fn.transact({'from':who,'gas':3_000_000}))
        self.assertEqual((r.status,len(r.logs)),(0,0))
        self.assertEqual(self.state(token),before)
        self.assert_backed()

    def test_listing_is_non_custodial_and_exact_purchase_records_receipt(self):
        self.list_card()
        self.assertEqual(self.nft.functions.ownerOf(1).call(),self.maker)
        self.assertEqual(self.nft.functions.balanceOf(self.market.address).call(),0)
        self.assertEqual(self.bal(self.B,self.market.address),0)
        r=self.send(self.market.functions.takeListing(1,1,101*E18),self.taker)
        self.assertEqual(self.nft.functions.ownerOf(1).call(),self.taker)
        self.assertEqual(self.market.functions.activeListing(1).call(),0)
        self.assertEqual(self.market.functions.getListing(1).call()[4],1)
        self.assertEqual(self.bal(self.B,self.maker),10_100*E18)
        self.assertEqual(self.bal(self.B,self.taker),9_899*E18)
        self.assertEqual(self.bal(self.B,self.fee_recipient),E18)
        self.assertEqual(self.market.functions.getListingReceipt(1).call(),(100*E18,E18,1,r.blockNumber))
        event,=self.market.events.Trade().process_receipt(r,errors=DISCARD)
        self.assertEqual((event['args']['orderId'],event['args']['tokenId'],event['args']['isBid']),(1,1,False))
        self.assert_backed()

    def test_partial_bid_releases_maker_fee_reserve_and_cancel_remainder(self):
        self.bid(cards=3)
        self.assertEqual(self.bal(self.B,self.market.address),303*E18)
        r=self.send(self.market.functions.sellToBid(1,1,99*E18),self.maker)
        self.assertEqual(self.market.functions.getBid(1).call()[4:6],(2,0))
        self.assertEqual(self.bal(self.B,self.maker),10_099*E18)
        self.assertEqual(self.bal(self.B,self.taker),9_698*E18)
        self.assertEqual(self.market.functions.getBidReceipt(1).call(),(100*E18,E18,1,r.blockNumber))
        self.assert_backed()
        self.send(self.market.functions.cancelBid(1),self.taker)
        self.assertEqual(self.bal(self.B,self.taker),9_900*E18)
        self.assertEqual(self.market.functions.getBid(1).call()[4:6],(0,2))
        self.assert_backed()

    def test_partial_bid_expiry_is_permissionless_and_no_fee_on_refund(self):
        self.bid(cards=2,duration=601)
        self.send(self.market.functions.sellToBid(1,1,99*E18),self.maker)
        self.expect_revert('NotExpired',self.market.functions.expireBid(1),**{'from':self.third})
        self.warp(602)
        self.assert_atomic_revert(self.market.functions.sellToBid(1,2,99*E18),self.maker,token=2,error='Closed')
        self.send(self.market.functions.expireBid(1),self.third)
        self.assertEqual(self.market.functions.getBid(1).call()[5],3)
        self.assertEqual(self.bal(self.B,self.taker),9_900*E18)
        self.assertEqual(self.bal(self.B,self.fee_recipient),E18)
        self.assert_backed()

    def test_revoked_approval_restores_listing_until_explicit_cancel(self):
        self.list_card()
        self.nft.functions.setApprovalForAll(self.market.address,False).transact({'from':self.maker})
        self.assertFalse(self.market.functions.listingValid(1).call())
        self.assert_atomic_revert(self.market.functions.takeListing(1,1,101*E18),self.taker,error='NotApproved')
        self.nft.functions.approve(self.market.address,1).transact({'from':self.maker})
        self.assertTrue(self.market.functions.listingValid(1).call())
        self.send(self.market.functions.cancelListing(1),self.maker)
        self.assert_atomic_revert(self.market.functions.takeListing(1,1,101*E18),self.taker,error='Closed')
        self.assertEqual(self.nft.functions.ownerOf(1).call(),self.maker)

    def test_pruned_approval_cannot_reactivate_and_new_listing_has_new_id(self):
        self.list_card()
        self.expect_revert('StillValid',self.market.functions.pruneInvalidListing(1),**{'from':self.third})
        self.nft.functions.setApprovalForAll(self.market.address,False).transact({'from':self.maker})
        self.send(self.market.functions.pruneInvalidListing(1),self.third)
        self.nft.functions.setApprovalForAll(self.market.address,True).transact({'from':self.maker})
        self.assertFalse(self.market.functions.listingValid(1).call())
        self.list_card(price=150*UNIT)
        self.assertEqual(self.market.functions.activeListing(1).call(),2)
        self.assert_atomic_revert(self.market.functions.takeListing(1,1,1_000*E18),self.taker,error='Closed')
        self.assert_atomic_revert(self.market.functions.takeListing(2,1,101*E18),self.taker,error='PriceChanged')
        self.send(self.market.functions.takeListing(2,1,1515*E18//10),self.taker)
        self.assert_backed()

    def test_ownership_roundtrip_can_restore_unpruned_listing(self):
        self.list_card()
        self.nft.functions.transferFrom(self.maker,self.third,1).transact({'from':self.maker})
        self.assertFalse(self.market.functions.listingValid(1).call())
        self.assert_atomic_revert(self.market.functions.takeListing(1,1,101*E18),self.taker,error='NotOwner')
        self.nft.functions.transferFrom(self.third,self.maker,1).transact({'from':self.third})
        self.assertTrue(self.market.functions.listingValid(1).call())
        self.send(self.market.functions.takeListing(1,1,101*E18),self.taker)
        self.assert_backed()

    def test_new_owner_can_replace_stale_listing_without_touching_current_id(self):
        self.list_card()
        self.nft.functions.transferFrom(self.maker,self.third,1).transact({'from':self.maker})
        self.list_card(who=self.third,price=110*UNIT)
        self.assertEqual(self.market.functions.getListing(1).call()[4],4)
        self.assertEqual(self.market.functions.activeListing(1).call(),2)
        self.expect_revert('Closed',self.market.functions.cancelListing(1),**{'from':self.maker})
        self.assertEqual(self.market.functions.activeListing(1).call(),2)
        self.send(self.market.functions.takeListing(2,1,1111*E18//10),self.taker)
        self.assertEqual(self.bal(self.B,self.third),10_110*E18)
        self.assert_backed()

    def test_listing_expiry_duplicate_wrong_id_and_self_trade_guards(self):
        self.list_card(duration=601)
        self.expect_revert('AlreadyListed',self.market.functions.list(1,100*UNIT,self.now()+DAY),**{'from':self.maker})
        self.assert_atomic_revert(self.market.functions.takeListing(1,2,101*E18),self.taker,error='WrongToken')
        self.assert_atomic_revert(self.market.functions.takeListing(1,1,101*E18),self.maker,error='SelfTrade')
        self.expect_revert('NoOrder',self.market.functions.takeListing(99,1,101*E18),**{'from':self.taker})
        self.expect_revert('NotMaker',self.market.functions.cancelListing(1),**{'from':self.third})
        self.warp(602)
        self.assert_atomic_revert(self.market.functions.takeListing(1,1,101*E18),self.taker,error='Closed')
        self.send(self.market.functions.pruneInvalidListing(1),self.third)
        self.assertEqual(self.market.functions.getListing(1).call()[4],3)
        self.assertEqual(self.market.functions.activeListing(1).call(),0)

    def test_locked_card_rolls_back_bid_and_listing_without_pruning_on_guess(self):
        self.list_card()
        self.bid(cards=1)
        self.nft.functions.setLocked(1,True).transact()
        self.assertTrue(self.market.functions.listingValid(1).call())
        self.expect_revert('StillValid',self.market.functions.pruneInvalidListing(1),**{'from':self.third})
        self.assert_atomic_revert(self.market.functions.takeListing(1,1,101*E18),self.taker)
        self.assert_atomic_revert(self.market.functions.sellToBid(1,1,99*E18),self.maker)
        self.nft.functions.setLocked(1,False).transact()
        self.send(self.market.functions.sellToBid(1,1,99*E18),self.maker)
        self.assertEqual(self.market.functions.getListing(1).call()[4],4)
        self.assert_backed()

    def test_unavailable_owner_or_approval_read_does_not_prune_listing(self):
        self.list_card()
        for owner_failure in (True,False):
            self.nft.functions.setReadFailures(owner_failure,not owner_failure).transact()
            fn=self.market.functions.pruneInvalidListing(1)
            r=self.w3.eth.wait_for_transaction_receipt(fn.transact({'from':self.third,'gas':500_000}))
            self.assertEqual((r.status,len(r.logs)),(0,0))
            self.assertEqual(self.market.functions.getListing(1).call()[4],0)
            self.assertEqual(self.market.functions.activeListing(1).call(),1)
        self.nft.functions.setReadFailures(False,False).transact()
        self.assertTrue(self.market.functions.listingValid(1).call())

    def test_receiver_rejection_atomicity_and_independent_bid_not_blocked(self):
        receiver=self.receiver(reject=True)
        self.list_card()
        self.assert_atomic_revert(self.forwarded(receiver,self.market.functions.takeListing(1,1,101*E18)),self.deployer)
        self.forward(receiver,self.market.functions.placeBid(100*UNIT,1,self.now()+DAY))
        self.bid(cards=1,who=self.taker,price=99*UNIT)
        self.assert_atomic_revert(self.market.functions.sellToBid(1,1,99*E18),self.maker)
        self.send(self.market.functions.sellToBid(2,1,98*E18),self.maker)
        self.assertEqual(self.nft.functions.ownerOf(1).call(),self.taker)
        self.assertEqual(self.market.functions.getBid(1).call()[4],1)
        self.assert_backed()

    def test_receiver_reentry_fails_while_valid_listing_purchase_succeeds(self):
        receiver=self.receiver()
        self.list_card()
        data=bytes.fromhex(self.market.functions.takeListing(1,1,101*E18)._encode_transaction_data()[2:])
        receiver.functions.configure(False,self.market.address,data,ZERO).transact()
        self.forward(receiver,self.market.functions.takeListing(1,1,101*E18))
        self.assertTrue(receiver.functions.attempted().call())
        self.assertFalse(receiver.functions.reentrySucceeded().call())
        self.assertEqual(receiver.functions.lastErr().call(),Web3.keccak(text='Reentrancy()')[:4])
        self.assertEqual(self.market.functions.tradeCount().call(),1)
        self.assertEqual(self.nft.functions.ownerOf(1).call(),receiver.address)
        self.assert_backed()

    def test_bid_receiver_reentry_cannot_cancel_bid_or_double_refund(self):
        receiver=self.receiver()
        self.forward(receiver,self.market.functions.placeBid(100*UNIT,2,self.now()+DAY))
        data=bytes.fromhex(self.market.functions.cancelBid(1)._encode_transaction_data()[2:])
        receiver.functions.configure(False,self.market.address,data,ZERO).transact()
        self.send(self.market.functions.sellToBid(1,1,99*E18),self.maker)
        self.assertFalse(receiver.functions.reentrySucceeded().call())
        self.assertEqual(receiver.functions.lastErr().call(),Web3.keccak(text='Reentrancy()')[:4])
        self.assertEqual(self.market.functions.getBid(1).call()[4:6],(1,0))
        self.assert_backed()

    def test_receiver_forwarding_card_away_reverts_owner_postcondition(self):
        receiver=self.receiver()
        receiver.functions.configure(False,ZERO,b'',self.fourth).transact()
        self.list_card()
        self.assert_atomic_revert(self.forwarded(receiver,self.market.functions.takeListing(1,1,101*E18)),self.deployer,error='TransferMismatch')
        self.forward(receiver,self.market.functions.placeBid(100*UNIT,1,self.now()+DAY))
        self.assert_atomic_revert(self.market.functions.sellToBid(1,1,99*E18),self.maker,error='TransferMismatch')

    def test_sender_receiver_tax_deposits_and_listing_purchase_revert_everything(self):
        self.list_card()
        for sender_tax in (True,False):
            self.B.functions.setTax(100 if sender_tax else 0,0 if sender_tax else 100).transact()
            self.assert_atomic_revert(self.market.functions.placeBid(100*UNIT,1,self.now()+DAY),self.taker,error='TransferMismatch')
            self.assert_atomic_revert(self.market.functions.takeListing(1,1,101*E18),self.taker,error='TransferMismatch')
        self.B.functions.setTax(0,0).transact()

    def test_quote_tax_after_bid_deposit_rolls_back_nft_and_bid(self):
        self.bid(cards=2)
        self.list_card()
        for sender_tax in (True,False):
            self.B.functions.setTax(100 if sender_tax else 0,0 if sender_tax else 100).transact()
            self.assert_atomic_revert(self.market.functions.sellToBid(1,1,99*E18),self.maker,error='TransferMismatch')
        self.B.functions.setTax(0,0).transact()
        self.send(self.market.functions.sellToBid(1,1,99*E18),self.maker)
        self.assert_backed()

    def test_blocked_seller_payout_reverts_nft_transfer_and_all_accounting(self):
        self.bid(cards=1)
        self.list_card()
        self.B.functions.setBlocked(self.maker,True).transact()
        self.assert_atomic_revert(self.market.functions.takeListing(1,1,101*E18),self.taker,error='TransferFailed')
        self.assert_atomic_revert(self.market.functions.sellToBid(1,1,99*E18),self.maker,error='TransferFailed')
        self.assertEqual(self.market.functions.claimable(self.maker).call(),0)

    def test_fee_recipient_blocked_defers_fee_for_listing_and_bid(self):
        self.list_card()
        self.bid(cards=2)
        self.B.functions.setBlocked(self.fee_recipient,True).transact()
        self.send(self.market.functions.takeListing(1,1,101*E18),self.third)
        self.send(self.market.functions.sellToBid(1,2,99*E18),self.maker)
        self.assertEqual(self.market.functions.claimable(self.fee_recipient).call(),2*E18)
        self.assert_backed()
        self.B.functions.setBlocked(self.fee_recipient,False).transact()
        before=self.bal(self.B,self.fourth)
        self.send(self.market.functions.withdraw(self.fee_recipient),self.fourth)
        self.assertEqual(self.bal(self.B,self.fee_recipient),2*E18)
        self.assertEqual(self.bal(self.B,self.fourth),before)
        self.assert_backed()

    def test_blocked_buyer_reserve_and_cancel_refund_are_fixed_claims(self):
        self.bid(cards=2)
        self.B.functions.setBlocked(self.taker,True).transact()
        self.send(self.market.functions.sellToBid(1,1,99*E18),self.maker)
        self.assertEqual(self.market.functions.claimable(self.taker).call(),E18)
        self.send(self.market.functions.cancelBid(1),self.taker)
        self.assertEqual(self.market.functions.claimable(self.taker).call(),102*E18)
        self.assert_backed()
        self.assert_atomic_revert(self.market.functions.withdraw(self.taker),self.third,error='TransferFailed')
        self.B.functions.setBlocked(self.taker,False).transact()
        before=self.bal(self.B,self.third)
        self.send(self.market.functions.withdraw(self.taker),self.third)
        self.assertEqual(self.bal(self.B,self.taker),9_900*E18)
        self.assertEqual(self.bal(self.B,self.third),before)
        self.expect_revert('ZeroAmount',self.market.functions.withdraw(self.taker),**{'from':self.third})
        self.assert_backed()

    def test_blocked_expiry_refund_stays_backed_until_permissionless_withdraw(self):
        self.bid(cards=2,duration=601)
        self.B.functions.setBlocked(self.taker,True).transact()
        self.warp(602)
        self.send(self.market.functions.expireBid(1),self.third)
        self.assertEqual(self.market.functions.claimable(self.taker).call(),202*E18)
        self.assert_backed()
        self.B.functions.setBlocked(self.taker,False).transact()
        self.send(self.market.functions.withdraw(self.taker),self.fourth)
        self.assertEqual(self.bal(self.B,self.taker),10_000*E18)
        self.assert_backed()

    def test_bid_guards_amount_bound_self_trade_and_external_delivery(self):
        self.bid(cards=1,who=self.maker)
        self.assert_atomic_revert(self.market.functions.sellToBid(1,1,99*E18),self.maker,error='SelfTrade')
        self.assert_atomic_revert(self.market.functions.sellToBid(1,101,99*E18+1),self.third,token=101,error='PriceChanged')
        self.expect_revert('NotMaker',self.market.functions.cancelBid(1),**{'from':self.third})
        self.expect_revert('OnlySelf',self.market.functions.deliver(self.third,101*E18),**{'from':self.third})
        self.assert_backed()

    def test_dai_callback_reentry_is_rejected_without_double_order(self):
        data=bytes.fromhex(self.market.functions.placeBid(100*UNIT,1,self.now()+DAY)._encode_transaction_data()[2:])
        self.B.functions.arm(self.market.address,data).transact()
        self.bid(cards=1)
        self.assertFalse(self.B.functions.reentrySucceeded().call())
        self.assertEqual(self.B.functions.lastErr().call(),Web3.keccak(text='Reentrancy()')[:4])
        self.assertEqual(self.market.functions.bidCount().call(),1)
        self.assert_backed()

    def test_pages_above_sixteen_preserve_ids_history_and_trade_receipts(self):
        for token in range(1,19):
            self.list_card(token=token,price=UNIT)
            self.bid(cards=1,price=UNIT)
            self.send(self.market.functions.takeListing(token,token,101*E18//100),self.third)
        for name in ('getListings','getBids','getTrades'):
            getter=getattr(self.market.functions,name)
            ids,rows,next_cursor=getter(0,16).call()
            self.assertEqual(ids,list(range(1,17)))
            self.assertEqual((len(rows),next_cursor),(16,17))
            ids2,rows2,next_cursor=getter(next_cursor,16).call()
            self.assertEqual((ids2,len(rows2),next_cursor),([17,18],2,0))
            self.assertEqual(getter(19,16).call(),[[],[],0])
            self.expect_revert('BadPageSize',getter(0,17),**{'from':self.third})
        refs,next_cursor=self.market.functions.getMyOrders(self.maker,0,16).call()
        self.assertEqual([tuple(ref) for ref in refs],[(False,i) for i in range(1,17)])
        self.assertEqual(next_cursor,16)
        tail,end=self.market.functions.getMyOrders(self.maker,next_cursor,16).call()
        self.assertEqual(([tuple(ref) for ref in tail],end),([(False,17),(False,18)],0))
        self.assertEqual(self.market.functions.getMyOrders(self.fourth,0,16).call(),[[],0])
        self.assert_backed()

    def test_constructor_validation_fixed_fee_and_uint_bounds(self):
        self.assertEqual(self.market.functions.FEE_BPS().call(),100)
        self.assertEqual(self.market.functions.FEE_AUTHORITY().call(),ZERO)
        art=ART['AwakeNftMarket'];factory=self.w3.eth.contract(abi=art['abi'],bytecode=art['bin'])
        quote9=self.deploy('TokenBookMock',9)
        self.expect_revert('BadTokens',factory.constructor(self.nft.address,quote9.address,self.fee_recipient),**{'from':self.deployer})
        self.expect_revert('BadFeeRecipient',factory.constructor(self.nft.address,self.B.address,ZERO),**{'from':self.deployer})
        self.nft.functions.setInterfaceSupported(False).transact()
        self.expect_revert('BadTokens',factory.constructor(self.nft.address,self.B.address,self.fee_recipient),**{'from':self.deployer})
        self.nft.functions.setInterfaceSupported(True).transact()
        gross=(2**64-1)*10**12*(2**32-1)
        self.B.functions.mint(self.taker,gross+gross//100).transact()
        self.bid(cards=2**32-1,price=2**64-1)
        self.assertEqual(self.market.functions.reservedDAI().call(),gross+gross//100)
        self.send(self.market.functions.cancelBid(1),self.taker)
        self.assert_backed()
