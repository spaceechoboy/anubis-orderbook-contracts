"""EOA-signed direct settlement: real local EVM, independent EIP-712 encoding."""
from copy import deepcopy
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3, EthereumTesterProvider
from web3.logs import DISCARD
from test_escrow import Base, ART, DAY, E18

ZERO = '0x' + '0'*40
B32 = bytes(32)
U64 = 2**64 - 1
ORDER_FIELDS = [
 ('maker','address'),('makerNonce','uint256'),('makerEpoch','uint256'),
 ('marketId','bytes32'),('assetKind','uint8'),('baseToken','address'),('quoteToken','address'),
 ('baseDecimals','uint8'),('buy','bool'),('priceTicks','uint64'),('totalLots','uint64'),
 ('minFillLots','uint64'),('allowPartial','bool'),('nftMode','uint8'),('tokenId','uint256'),
 ('validAfter','uint64'),('expiresAt','uint64'),('feeBps','uint16'),('feeRecipient','address'),
 ('gateSigner','address'),('inviteDigest','bytes32'),('singleCounterparty','bool'),('deploymentId','bytes32')]
PERMIT_FIELDS = [('orderHash','bytes32'),('taker','address'),('fillLots','uint64'),
 ('expectedFilledLots','uint64'),('permitNonce','uint256'),('issuedAt','uint64'),
 ('expiresAt','uint64'),('chainId','uint256'),('verifyingContract','address'),('tokenId','uint256')]
TYPES = {name:[{'name':k,'type':t} for k,t in fields] for name,fields in [('Order',ORDER_FIELDS),('GatePermit',PERMIT_FIELDS)]}

class SettlementBase(Base):
    def setUp(self):
        self.assertTrue('NoncustodialSettlement' in ART, 'new direct-settlement contract must be compiled')
        self.w3=Web3(EthereumTesterProvider()); self.t=self.w3.provider.ethereum_tester
        self.deployer,self.maker,self.taker,self.third,self.feeRcv,self.gate=self.w3.eth.accounts[:6]
        self.keys={Account.from_key(k).address:k for k in self.t.backend.account_keys}
        self.A=self.deploy('SettlementToken',18); self.B=self.deploy('SettlementToken',18)
        self.nft=self.deploy('NftMarketMock'); self.nft.functions.mintMany(self.maker,1,12).transact()
        self.nft.functions.mintMany(self.taker,101,12).transact()
        self.serial=0
        self.market=self.new_market()
        self.seed(self.market,self.A)

    def new_market(self,base=None,decimals=18,kind=0,fee=None,gate=None):
        return self.deploy('NoncustodialSettlement',(base or self.A).address,self.B.address,decimals,kind,fee or self.feeRcv,Web3.keccak(text='local-deployment-v1'),gate or self.gate)

    def seed(self,market,base=None):
        base=base or self.A
        for who in (self.maker,self.taker,self.third):
            for token in (base,self.B):
                token.functions.mint(who,10**36).transact()
                token.functions.approve(market.address,2**256-1).transact({'from':who})

    def domain(self,market=None):
        return {'name':'Anubis Invite Settlement','version':'1','chainId':self.w3.eth.chain_id,'verifyingContract':(market or self.market).address}

    def signed(self,name,value,signer,market=None,domain=None):
        msg=encode_typed_data(domain_data=domain or self.domain(market),message_types={name:TYPES[name]},message_data=value)
        return Account.sign_message(msg,self.keys[signer]).signature

    def digest(self,name,value,market=None,domain=None):
        msg=encode_typed_data(domain_data=domain or self.domain(market),message_types={name:TYPES[name]},message_data=value)
        return Web3.keccak(b'\x19'+msg.version+msg.header+msg.body)

    def order(self,market=None,**changes):
        m=market or self.market; self.serial+=1; now=self.now()
        value=dict(maker=self.maker,makerNonce=self.serial,makerEpoch=m.functions.epoch(self.maker).call(),
            marketId=m.functions.MARKET_ID().call(),assetKind=m.functions.ASSET_KIND().call(),
            baseToken=m.functions.BASE_TOKEN().call(),quoteToken=self.B.address,baseDecimals=m.functions.BASE_DECIMALS().call(),
            buy=False,priceTicks=2_000_000,totalLots=100_000_000,minFillLots=1,allowPartial=True,
            nftMode=0,tokenId=0,validAfter=now,expiresAt=now+DAY,feeBps=100,feeRecipient=m.functions.FEE_RECIPIENT().call(),
            gateSigner=self.gate,inviteDigest=Web3.keccak(text='invitation-'+str(self.serial)),singleCounterparty=True,
            deploymentId=m.functions.DEPLOYMENT_ID().call())
        value.update(changes); return value

    def permit(self,o,lots=None,market=None,**changes):
        m=market or self.market; self.serial+=1; now=self.now(); h=self.digest('Order',o,m)
        value=dict(orderHash=h,taker=self.taker,fillLots=lots if lots is not None else o['totalLots'],
            expectedFilledLots=m.functions.filledLots(h).call(),permitNonce=self.serial,issuedAt=now,
            expiresAt=min(now+300,o['expiresAt']),chainId=self.w3.eth.chain_id,verifyingContract=m.address,tokenId=o['tokenId'])
        value.update(changes);return value

    def call_fill(self,o,p=None,market=None,msig=None,psig=None,max_debit=2**256-1,min_net=0):
        m=market or self.market;p=p or self.permit(o,market=m)
        return m.functions.fill(tuple(o[k] for k,_ in ORDER_FIELDS),msig if msig is not None else self.signed('Order',o,o['maker'],m),
            tuple(p[k] for k,_ in PERMIT_FIELDS),psig if psig is not None else self.signed('GatePermit',p,o['gateSigner'],m),max_debit,min_net)

    def send(self,fn,who=None):
        r=self.w3.eth.wait_for_transaction_receipt(fn.transact({'from':who or self.taker}));self.assertEqual(r.status,1);return r

    def snapshot(self,o,p,market=None,base=None):
        m=market or self.market;base=base or self.A;h=self.digest('Order',o,m);pd=self.digest('GatePermit',p,m)
        actors=list(dict.fromkeys([self.maker,self.taker,self.third,self.feeRcv,m.address,o['maker'],p['taker'],o['feeRecipient']]))
        return ([base.functions.balanceOf(a).call() for a in actors], [self.B.functions.balanceOf(a).call() for a in actors],
            [self.B.functions.allowance(a,m.address).call() for a in actors],
            [base.functions.allowance(a,m.address).call() for a in actors] if m.functions.ASSET_KIND().call()==0 else [base.functions.ownerOf(i).call() for i in (1,2,101,102)],
            m.functions.filledLots(h).call(),m.functions.boundTaker(h).call(),
            m.functions.nonceToOrderHash(o['maker'],o['makerNonce']).call(),m.functions.usedPermit(pd).call(),
            m.functions.cancelled(h).call(),m.functions.cancelledNonce(o['maker'],o['makerNonce']).call(),m.functions.epoch(o['maker']).call())

    def atomic_revert(self,o,p=None,market=None,base=None,fn=None,who=None):
        m=market or self.market;p=p or self.permit(o,market=m);before=self.snapshot(o,p,m,base)
        r=self.w3.eth.wait_for_transaction_receipt((fn or self.call_fill(o,p,m)).transact({'from':who or self.taker,'gas':4_000_000}))
        self.assertEqual((r.status,len(r.logs)),(0,0));self.assertEqual(self.snapshot(o,p,m,base),before)


class TestDirectSettlement(SettlementBase):
    def test_partial_fill_cancel_leaves_all_unfilled_assets_in_wallet(self):
        o=self.order();p=self.permit(o,40_000_000);initialA=self.bal(self.A,self.maker);initialB=self.bal(self.B,self.taker)
        # 40 tokens * 2 DAI = 80 DAI + 0.8 DAI fee; no escrow funding.
        r=self.send(self.call_fill(o,p,max_debit=80_800_000_000_000_000_000))
        self.assertEqual(initialA-self.bal(self.A,self.maker),40*E18)
        self.assertEqual(initialB-self.bal(self.B,self.taker),80_800_000_000_000_000_000)
        self.assertEqual(self.bal(self.B,self.feeRcv),800_000_000_000_000_000)
        self.assertEqual(self.market.functions.filledLots(p['orderHash']).call(),40_000_000)
        event,=self.market.events.Filled().process_receipt(r,errors=DISCARD)
        self.assertEqual((event['args']['gross'],event['args']['fee']),(80*E18,800_000_000_000_000_000))
        self.send(self.market.functions.cancel(tuple(o[k] for k,_ in ORDER_FIELDS)),self.maker)
        self.atomic_revert(o,self.permit(o,60_000_000))
        self.assertEqual((self.bal(self.A,self.market.address),self.bal(self.B,self.market.address)),(0,0))
        self.assertEqual(initialA-self.bal(self.A,self.maker),40*E18)

    def test_order_cannot_select_another_valid_gate_key(self):
        o=self.order(gateSigner=self.third);p=self.permit(o)
        self.atomic_revert(o,p)

    def test_five_minute_permit_allows_mobile_wallet_roundtrip(self):
        o=self.order();p=self.permit(o,expiresAt=self.now()+300)
        r=self.w3.eth.wait_for_transaction_receipt(self.call_fill(o,p).transact({'from':self.taker,'gas':4_000_000}))
        self.assertEqual(r.status,1)

    def test_buy_maker_spends_gross_taker_receives_net(self):
        o=self.order(buy=True);p=self.permit(o);mb=self.bal(self.B,self.maker);tb=self.bal(self.B,self.taker);ta=self.bal(self.A,self.taker)
        self.send(self.call_fill(o,p,min_net=198*E18))
        self.assertEqual((mb-self.bal(self.B,self.maker),self.bal(self.B,self.taker)-tb,ta-self.bal(self.A,self.taker)),(200*E18,198*E18,100*E18))
        self.assertEqual(self.bal(self.B,self.feeRcv),2*E18)

    def test_expiry_boundary_has_no_refund_transaction_or_custody(self):
        o=self.order(expiresAt=self.now()+40);p=self.permit(o,40_000_000);self.send(self.call_fill(o,p));p=self.permit(o,60_000_000)
        self.t.time_travel(o['expiresAt']);self.t.mine_block()
        self.assertEqual(self.now(),o['expiresAt'])
        with self.assertRaisesRegex(Exception,'execution reverted'):
            self.call_fill(o,p).call({'from':self.taker},block_identifier='latest')
        self.atomic_revert(o,p)
        self.assertEqual((self.bal(self.A,self.market.address),self.bal(self.B,self.market.address)),(0,0))
        self.assertGreater(self.A.functions.allowance(self.maker,self.market.address).call(),0)

    def test_window_above_24h_and_future_start_are_not_executable(self):
        for changes in [dict(expiresAt=self.now()+DAY+1),dict(validAfter=self.now()+100,expiresAt=self.now()+200)]:
            o=self.order(**changes);self.atomic_revert(o)

    def test_24h_exact_window_is_allowed(self):
        o=self.order();self.send(self.call_fill(o))

    def test_cancel_nonce_and_epoch_do_not_require_gate_or_oracle(self):
        o=self.order();self.send(self.market.functions.cancelNonce(o['makerNonce']),self.maker);self.atomic_revert(o)
        o2=self.order();self.send(self.market.functions.cancelAll(7),self.maker);self.atomic_revert(o2)
        self.expect_revert('BadEpoch',self.market.functions.cancelAll(7),**{'from':self.maker})
        o3=self.order();self.send(self.call_fill(o3))

    def test_foreign_cancel_cannot_cancel_maker_order(self):
        o=self.order();self.expect_revert('NotMaker',self.market.functions.cancel(tuple(o[k] for k,_ in ORDER_FIELDS)),**{'from':self.third})
        self.send(self.market.functions.cancelNonce(o['makerNonce']),self.third);self.send(self.call_fill(o))

    def test_repeated_nonce_cannot_change_order_after_partial_fill(self):
        o=self.order();self.send(self.call_fill(o,self.permit(o,1)));changed={**o,'priceTicks':3_000_000}
        self.atomic_revert(changed)

    def test_allowance_revoke_and_restore_does_not_revive_cancelled_order(self):
        o=self.order();self.A.functions.approve(self.market.address,0).transact({'from':self.maker});self.atomic_revert(o)
        self.send(self.market.functions.cancel(tuple(o[k] for k,_ in ORDER_FIELDS)),self.maker)
        self.A.functions.approve(self.market.address,2**256-1).transact({'from':self.maker});self.atomic_revert(o)

    def test_split_fills_match_whole_200_lots_without_rounding_gain(self):
        o=self.order(totalLots=200,priceTicks=1);mb=self.bal(self.B,self.maker);tb=self.bal(self.B,self.taker);ma=self.bal(self.A,self.maker)
        for _ in range(200):self.send(self.call_fill(o,self.permit(o,1)))
        self.assertEqual((self.bal(self.B,self.maker)-mb,tb-self.bal(self.B,self.taker),self.bal(self.B,self.feeRcv),ma-self.bal(self.A,self.maker)),(200_000_000,202_000_000,2_000_000,200*10**12))
        self.atomic_revert(o,self.permit(o,1))

    def test_lgns_nine_decimal_lots_match_economic_amount(self):
        token=self.deploy('SettlementToken',9);m=self.new_market(token,9);self.seed(m,token)
        o=self.order(m);p=self.permit(o,market=m);before=self.bal(token,self.maker)
        self.send(self.call_fill(o,p,m));self.assertEqual(before-self.bal(token,self.maker),100*10**9)
        self.assertEqual(self.bal(self.B,self.feeRcv),2*E18)

    def test_minimum_fill_dust_full_only_and_overfill(self):
        cases=[(dict(minFillLots=20_000_000),10_000_000),(dict(minFillLots=20_000_000),90_000_000),(dict(allowPartial=False),40_000_000),({},100_000_001)]
        for changes,amount in cases:
            o=self.order(**changes);self.atomic_revert(o,self.permit(o,amount))
        o=self.order(minFillLots=20_000_000);self.send(self.call_fill(o,self.permit(o,80_000_000)));self.send(self.call_fill(o,self.permit(o,20_000_000)))

    def test_stale_permit_and_changed_taker_cannot_reuse_partial_order(self):
        o=self.order();p=self.permit(o,40_000_000);self.send(self.call_fill(o,p));self.atomic_revert(o,p)
        p2=self.permit(o,60_000_000,taker=self.third);self.atomic_revert(o,p2,who=self.third)
        p3=self.permit(o,60_000_000);self.atomic_revert(o,p3,who=self.third)

    def test_wrong_signer_and_domain_cannot_spend_approved_tokens(self):
        o=self.order();p=self.permit(o)
        for signature in [b'',bytes(65),self.signed('Order',o,self.third),self.signed('Order',o,self.maker,domain={**self.domain(),'chainId':6714}),self.signed('Order',o,self.maker,domain={**self.domain(),'verifyingContract':self.third})]:
            self.atomic_revert(o,p,fn=self.call_fill(o,p,msig=signature))
        self.atomic_revert(o,p,fn=self.call_fill(o,p,psig=self.signed('GatePermit',p,self.third)))

    def test_signed_immutable_fields_cannot_override_market_or_fee(self):
        for field,value in [('baseToken',self.B.address),('quoteToken',self.A.address),('baseDecimals',9),('assetKind',1),('marketId',bytes.fromhex('01'*32)),('feeBps',0),('feeRecipient',self.third),('deploymentId',bytes.fromhex('02'*32)),('singleCounterparty',False),('gateSigner',ZERO),('inviteDigest',B32)]:
            o=self.order(**{field:value});p=self.permit(o)
            psig=self.signed('GatePermit',p,self.gate)
            self.atomic_revert(o,p,fn=self.call_fill(o,p,psig=psig))

    def test_permit_ttl_order_hash_chain_contract_and_bounds(self):
        o=self.order()
        for changes in [dict(expiresAt=self.now()+301),dict(issuedAt=self.now()+30),dict(expiresAt=self.now()),dict(orderHash=B32),dict(chainId=6714),dict(verifyingContract=self.third),dict(expectedFilledLots=1),dict(fillLots=0),dict(tokenId=1)]:
            p=self.permit(o,**changes);self.atomic_revert(o,p)

    def test_price_protection_and_alias_actual_net_limits(self):
        o=self.order();p=self.permit(o);self.atomic_revert(o,p,fn=self.call_fill(o,p,max_debit=202*E18-1))
        o=self.order(buy=True);p=self.permit(o);self.atomic_revert(o,p,fn=self.call_fill(o,p,min_net=198*E18+1))
        for buy in (False,True):
            for fee in (self.maker,self.taker):
                m=self.new_market(fee=fee);self.seed(m);o=self.order(m,buy=buy);p=self.permit(o,market=m)
                mb=self.bal(self.B,self.maker);tb=self.bal(self.B,self.taker)
                expected=200*E18 if fee==self.taker else (198*E18 if buy else 202*E18)
                self.send(self.call_fill(o,p,m,max_debit=expected,min_net=expected))
                self.assertEqual(abs(self.bal(self.B,self.taker)-tb),expected)
                self.assertEqual(abs(self.bal(self.B,self.maker)-mb),expected)
                self.assertEqual(self.bal(self.B,m.address),0)

    def test_transfer_failures_revert_all_state_balances_and_allowance(self):
        for token in (self.A,self.B):
            for mode in (1,3,4,5,6):
                o=self.order();p=self.permit(o);token.functions.setMode(mode).transact();self.atomic_revert(o,p);token.functions.setMode(0).transact()
            for taxes in [(100,0),(0,100)]:
                o=self.order();p=self.permit(o);token.functions.setTax(*taxes).transact();self.atomic_revert(o,p);token.functions.setTax(0,0).transact()
        self.B.functions.setBlocked(self.feeRcv,True).transact();self.atomic_revert(self.order());self.B.functions.setBlocked(self.feeRcv,False).transact()
        self.A.functions.setBlocked(self.maker,True).transact();self.atomic_revert(self.order())

    def test_empty_return_transfer_is_accepted_only_with_exact_balance_changes(self):
        self.A.functions.setMode(2).transact();self.B.functions.setMode(2).transact();self.send(self.call_fill(self.order()))
        self.assertEqual(self.bal(self.B,self.market.address),0)

    def test_gas_exhaustion_does_not_leave_one_sided_transfers(self):
        o=self.order();p=self.permit(o);before=self.snapshot(o,p)
        r=self.w3.eth.wait_for_transaction_receipt(self.call_fill(o,p).transact({'from':self.taker,'gas':90_000}))
        self.assertEqual(r.status,0);self.assertEqual(self.snapshot(o,p),before)

    def test_preexisting_accidental_donation_is_not_consumed_or_increased(self):
        self.A.functions.mint(self.market.address,77).transact();self.B.functions.mint(self.market.address,88).transact()
        self.send(self.call_fill(self.order()));self.assertEqual((self.bal(self.A,self.market.address),self.bal(self.B,self.market.address)),(77,88))

    def test_high_s_and_invalid_v_signatures_are_rejected(self):
        o=self.order();p=self.permit(o);sig=bytes(self.signed('Order',o,self.maker));n=0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
        high=sig[:32]+(n-int.from_bytes(sig[32:64],'big')).to_bytes(32,'big')+bytes([55-sig[64]])
        for altered in (high,sig[:64]+bytes([0]),sig[:64]+bytes([29])):self.atomic_revert(o,p,fn=self.call_fill(o,p,msig=altered))

    def test_uint64_extremes_have_no_overflow_or_undercharged_fee(self):
        o=self.order(totalLots=U64,priceTicks=U64);gross=U64*U64*10**6
        self.A.functions.mint(self.maker,U64*10**12).transact();self.B.functions.mint(self.taker,gross+gross//100).transact()
        mb=self.bal(self.B,self.maker);self.send(self.call_fill(o));self.assertEqual(self.bal(self.B,self.maker)-mb,gross);self.assertEqual(self.bal(self.B,self.feeRcv),gross//100)

    def test_constructor_rejects_unsupported_assets_and_fee_custody(self):
        for base,quote,decimals,kind,fee in [(self.A.address,self.A.address,18,0,self.feeRcv),(self.A.address,self.B.address,9,0,self.feeRcv),(self.A.address,self.B.address,18,2,self.feeRcv),(self.A.address,self.B.address,18,0,ZERO),(self.A.address,self.B.address,18,0,self.A.address),(self.A.address,self.B.address,18,0,self.B.address)]:
            with self.assertRaises(Exception):self.deploy('NoncustodialSettlement',base,quote,decimals,kind,fee,Web3.keccak(text='id'),self.gate)

        for gate in (ZERO,self.A.address,self.B.address):
            with self.assertRaises(Exception):self.new_market(gate=gate)


class TestNftDirectSettlement(SettlementBase):
    def nft_market(self):
        m=self.new_market(self.nft,0,1)
        for who in (self.maker,self.taker):
            self.B.functions.approve(m.address,2**256-1).transact({'from':who})
            self.nft.functions.setApprovalForAll(m.address,True).transact({'from':who})
        return m

    def test_specific_card_never_enters_contract_and_dai_is_atomic(self):
        m=self.nft_market();o=self.order(m,nftMode=1,tokenId=1,totalLots=1,minFillLots=1,allowPartial=False,priceTicks=100_000_000);p=self.permit(o,market=m)
        tb=self.bal(self.B,self.taker);self.send(self.call_fill(o,p,m,max_debit=101*E18))
        self.assertEqual(self.nft.functions.ownerOf(1).call(),self.taker);self.assertEqual(tb-self.bal(self.B,self.taker),101*E18)
        self.assertEqual((self.nft.functions.balanceOf(m.address).call(),self.bal(self.B,m.address)),(0,0))

    def test_collection_bid_partial_cancel_and_expiry_do_not_hold_dai(self):
        m=self.nft_market();o=self.order(m,buy=True,nftMode=2,totalLots=3,minFillLots=1,priceTicks=100_000_000)
        mb=self.bal(self.B,self.maker);p=self.permit(o,1,m,tokenId=101);self.send(self.call_fill(o,p,m,min_net=99*E18))
        self.assertEqual(self.nft.functions.ownerOf(101).call(),self.maker);self.assertEqual(mb-self.bal(self.B,self.maker),100*E18)
        self.send(m.functions.cancel(tuple(o[k] for k,_ in ORDER_FIELDS)),self.maker)
        self.atomic_revert(o,self.permit(o,1,m,tokenId=102),m,self.nft)
        self.assertEqual(self.bal(self.B,m.address),0)

    def test_specific_card_bid_and_expired_collection_remainder_keep_dai_in_wallet(self):
        m=self.nft_market()
        o=self.order(m,buy=True,nftMode=1,tokenId=101,totalLots=1,minFillLots=1,allowPartial=False,priceTicks=100_000_000)
        self.send(self.call_fill(o,market=m,min_net=99*E18));self.assertEqual(self.nft.functions.ownerOf(101).call(),self.maker)
        o=self.order(m,buy=True,nftMode=2,totalLots=3,minFillLots=1,priceTicks=100_000_000,expiresAt=self.now()+40)
        self.send(self.call_fill(o,self.permit(o,1,m,tokenId=102),m));p=self.permit(o,1,m,tokenId=103)
        mb=self.bal(self.B,self.maker)
        self.t.time_travel(o['expiresAt']);self.t.mine_block()
        with self.assertRaisesRegex(Exception,'execution reverted'):
            self.call_fill(o,p,m).call({'from':self.taker},block_identifier='latest')
        self.atomic_revert(o,p,m,self.nft);self.assertEqual(self.bal(self.B,self.maker),mb)
        self.assertEqual(self.nft.functions.ownerOf(103).call(),self.taker);self.assertEqual(self.bal(self.B,m.address),0)

    def test_card_lock_approval_and_wrong_id_leave_dai_and_ownership_untouched(self):
        m=self.nft_market();o=self.order(m,nftMode=1,tokenId=1,totalLots=1,minFillLots=1,allowPartial=False)
        self.nft.functions.setLocked(1,True).transact();self.atomic_revert(o,market=m,base=self.nft);self.assertEqual(self.nft.functions.ownerOf(1).call(),self.maker)
        self.nft.functions.setLocked(1,False).transact();self.nft.functions.setApprovalForAll(m.address,False).transact({'from':self.maker});self.atomic_revert(o,market=m,base=self.nft)
        self.nft.functions.approve(m.address,1).transact({'from':self.maker});self.atomic_revert(o,self.permit(o,1,m,tokenId=2),m,self.nft)
        self.send(self.call_fill(o,market=m))

    def test_nft_receiver_reject_and_forward_roll_back_every_leg(self):
        m=self.nft_market();receiver=self.deploy('NftMarketReceiver');self.B.functions.mint(receiver.address,1_000*E18).transact()
        receiver.functions.execute(self.B.address,bytes.fromhex(self.B.functions.approve(m.address,2**256-1)._encode_transaction_data()[2:])).transact()
        for reject,forward in [(True,ZERO),(False,self.third)]:
            receiver.functions.configure(reject,ZERO,b'',forward).transact()
            o=self.order(m,nftMode=1,tokenId=1,totalLots=1,minFillLots=1,allowPartial=False);p=self.permit(o,1,m,taker=receiver.address)
            fn=self.call_fill(o,p,m);wrapper=receiver.functions.execute(m.address,bytes.fromhex(fn._encode_transaction_data()[2:]))
            self.atomic_revert(o,p,m,self.nft,fn=wrapper,who=self.deployer);self.assertEqual(self.nft.functions.ownerOf(1).call(),self.maker)

    def test_nft_receiver_reentry_to_fill_and_cancel_is_blocked(self):
        m=self.nft_market();receiver=self.deploy('NftMarketReceiver');self.B.functions.mint(receiver.address,1_000*E18).transact()
        receiver.functions.execute(self.B.address,bytes.fromhex(self.B.functions.approve(m.address,2**256-1)._encode_transaction_data()[2:])).transact()
        for token in (1,2):
            o=self.order(m,nftMode=1,tokenId=token,totalLots=1,minFillLots=1,allowPartial=False);p=self.permit(o,1,m,taker=receiver.address);fn=self.call_fill(o,p,m)
            data=fn._encode_transaction_data() if token==1 else m.functions.cancelAll(1)._encode_transaction_data()
            receiver.functions.configure(False,m.address,bytes.fromhex(data[2:]),ZERO).transact()
            self.send(receiver.functions.execute(m.address,bytes.fromhex(fn._encode_transaction_data()[2:])),self.deployer)
            self.assertFalse(receiver.functions.reentrySucceeded().call());self.assertEqual(receiver.functions.lastErr().call(),Web3.keccak(text='Reentrant()')[:4])
            self.assertEqual(self.nft.functions.ownerOf(token).call(),receiver.address)
