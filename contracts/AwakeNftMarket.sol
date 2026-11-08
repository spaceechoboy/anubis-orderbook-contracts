// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;
interface INftMarketToken { function balanceOf(address) external view returns(uint256); function decimals() external view returns(uint8); }
interface IAwakeCard {
    function ownerOf(uint256) external view returns(address);
    function getApproved(uint256) external view returns(address);
    function isApprovedForAll(address,address) external view returns(bool);
    function supportsInterface(bytes4) external view returns(bool);
    function safeTransferFrom(address,address,uint256) external;
}
/// @notice Fixed 1% non-custodial card listings and collateralized collection bids.
/// @dev Listings can become valid again after approval restoration: cancel explicitly.
contract AwakeNftMarket {
    address public immutable TOKEN_A;
    address public immutable TOKEN_B;
    address public immutable FEE_RECIPIENT;
    uint16 public constant FEE_BPS = 100;
    uint16 public constant activeFeeBps = 100;
    uint16 public constant MAX_FEE_BPS = 100;
    address public constant FEE_AUTHORITY = address(0);
    uint64 public constant FEE_CHANGE_DELAY = 0;
    uint256 public constant PRICE_TICK = 1e12;
    uint256 public constant MAX_PAGE = 16;
    enum Status { Open, Filled, Cancelled, Expired, Invalid }
    struct Listing { address maker; uint256 tokenId; uint64 priceTicks; uint64 expiry; Status status; uint64 placedBlock; }
    struct Bid { address maker; uint64 priceTicks; uint64 expiry; uint32 cards; uint32 remaining; Status status; uint64 placedBlock; }
    struct Receipt { uint256 grossFilled; uint256 feePaid; uint32 filledCards; uint64 lastFillBlock; }
    struct OrderRef { bool isBid; uint256 id; }
    struct Fill { bool isBid; uint256 orderId; uint256 tokenId; address seller; address buyer; uint64 priceTicks; uint256 gross; uint256 fee; uint64 blockNumber; }
    uint256 public listingCount;
    uint256 public bidCount;
    uint256 public tradeCount;
    uint256 public mutationCount;
    uint256 public reservedDAI;
    uint256 public totalClaimable;
    mapping(uint256=>Listing) private _listings;
    mapping(uint256=>Bid) private _bids;
    mapping(uint256=>Receipt) private _listingReceipts;
    mapping(uint256=>Receipt) private _bidReceipts;
    mapping(uint256=>Fill) private _trades;
    mapping(address=>OrderRef[]) private _myOrders;
    mapping(uint256=>uint256) public activeListing;
    mapping(address=>uint256) public claimable;
    uint256 private _lock=1;
    event ListingChanged(uint256 indexed sequence,uint256 indexed id,address indexed maker,Status status);
    event BidChanged(uint256 indexed sequence,uint256 indexed id,address indexed maker,Status status);
    event Trade(uint256 indexed sequence,uint256 indexed tradeId,bool isBid,uint256 orderId,uint256 tokenId,address seller,address buyer,uint64 priceTicks,uint256 gross,uint256 fee);
    event PayoutDeferred(address indexed recipient,uint256 amount);
    event Withdrawn(address indexed recipient,uint256 amount);
    error BadTokens(); error BadFeeRecipient(); error Reentrancy(); error BadDuration(); error ZeroAmount();
    error NoOrder(); error Closed(); error NotMaker(); error NotExpired(); error NotOwner(); error NotApproved();
    error AlreadyListed(); error StillValid(); error SelfTrade(); error PriceChanged(); error WrongToken();
    error BadPageSize(); error TransferFailed(); error TransferMismatch(); error OnlySelf(); error InsufficientGas();
    modifier nonReentrant(){ if(_lock!=1) revert Reentrancy(); _lock=2; _; _lock=1; }
    constructor(address nft,address dai,address recipient){
        if(nft==dai || nft.code.length==0 || dai.code.length==0 || INftMarketToken(dai).decimals()!=18 || !IAwakeCard(nft).supportsInterface(0x80ac58cd)) revert BadTokens();
        if(recipient==address(0)||recipient==address(this)) revert BadFeeRecipient();
        TOKEN_A=nft; TOKEN_B=dai; FEE_RECIPIENT=recipient;
    }
    function _validExpiry(uint64 expiry) private view { if(expiry<block.timestamp+600 || expiry>block.timestamp+30 days) revert BadDuration(); }
    function _approved(address owner,uint256 tokenId) private view returns(bool){ return IAwakeCard(TOKEN_A).getApproved(tokenId)==address(this)||IAwakeCard(TOKEN_A).isApprovedForAll(owner,address(this)); }
    function _ownedApproved(address owner,uint256 tokenId) private view { if(IAwakeCard(TOKEN_A).ownerOf(tokenId)!=owner) revert NotOwner(); if(!_approved(owner,tokenId)) revert NotApproved(); }
    // Errors from an upgraded/restricted NFT are not proof of an invalid owner/approval.
    function listingValid(uint256 id) public view returns(bool){
        Listing storage x=_listings[id];
        if(x.maker==address(0)||x.status!=Status.Open||block.timestamp>=x.expiry) return false;
        return IAwakeCard(TOKEN_A).ownerOf(x.tokenId)==x.maker && _approved(x.maker,x.tokenId);
    }
    function list(uint256 tokenId,uint64 priceTicks,uint64 expiry) external nonReentrant returns(uint256 id){
        if(priceTicks==0) revert ZeroAmount(); _validExpiry(expiry); _ownedApproved(msg.sender,tokenId);
        uint256 old=activeListing[tokenId];
        if(old!=0){ if(listingValid(old)) revert AlreadyListed(); _closeListing(old,Status.Invalid); }
        id=++listingCount;
        _listings[id]=Listing(msg.sender,tokenId,priceTicks,expiry,Status.Open,uint64(block.number));
        activeListing[tokenId]=id; _myOrders[msg.sender].push(OrderRef(false,id));
        emit ListingChanged(++mutationCount,id,msg.sender,Status.Open);
    }
    function _listing(uint256 id) private view returns(Listing storage x){ x=_listings[id]; if(x.maker==address(0)) revert NoOrder(); if(x.status!=Status.Open) revert Closed(); }
    function _bid(uint256 id) private view returns(Bid storage x){ x=_bids[id]; if(x.maker==address(0)) revert NoOrder(); if(x.status!=Status.Open) revert Closed(); }
    function _closeListing(uint256 id,Status status) private { Listing storage x=_listings[id]; x.status=status; if(activeListing[x.tokenId]==id) delete activeListing[x.tokenId]; emit ListingChanged(++mutationCount,id,x.maker,status); }
    function cancelListing(uint256 id) external nonReentrant { Listing storage x=_listing(id); if(x.maker!=msg.sender) revert NotMaker(); _closeListing(id,Status.Cancelled); }
    function pruneInvalidListing(uint256 id) external nonReentrant { Listing storage x=_listing(id); if(listingValid(id)) revert StillValid(); _closeListing(id,block.timestamp>=x.expiry?Status.Expired:Status.Invalid); }
    function takeListing(uint256 id,uint256 expectedTokenId,uint256 maxDebit) external nonReentrant {
        Listing storage x=_listing(id);
        if(block.timestamp>=x.expiry) revert Closed(); if(msg.sender==x.maker) revert SelfTrade();
        if(x.tokenId!=expectedTokenId) revert WrongToken(); _ownedApproved(x.maker,x.tokenId);
        uint256 gross=uint256(x.priceTicks)*PRICE_TICK; uint256 fee=gross/100;
        if(gross+fee>maxDebit) revert PriceChanged();
        _closeListing(id,Status.Filled);
        _moveExact(msg.sender,address(this),gross+fee);
        IAwakeCard(TOKEN_A).safeTransferFrom(x.maker,msg.sender,x.tokenId);
        if(IAwakeCard(TOKEN_A).ownerOf(x.tokenId)!=msg.sender) revert TransferMismatch();
        _moveExact(address(this),x.maker,gross); _pay(FEE_RECIPIENT,fee);
        _record(false,id,x.tokenId,x.maker,msg.sender,x.priceTicks,gross,fee);
    }
    function placeBid(uint64 priceTicks,uint32 cards,uint64 expiry) external nonReentrant returns(uint256 id){
        if(priceTicks==0||cards==0) revert ZeroAmount(); _validExpiry(expiry);
        uint256 deposit=_reserve(priceTicks,cards); id=++bidCount;
        _bids[id]=Bid(msg.sender,priceTicks,expiry,cards,cards,Status.Open,uint64(block.number));
        _myOrders[msg.sender].push(OrderRef(true,id)); reservedDAI+=deposit;
        _moveExact(msg.sender,address(this),deposit); emit BidChanged(++mutationCount,id,msg.sender,Status.Open);
    }
    function sellToBid(uint256 id,uint256 tokenId,uint256 minNet) external nonReentrant {
        Bid storage x=_bid(id); if(block.timestamp>=x.expiry) revert Closed(); if(x.maker==msg.sender) revert SelfTrade();
        _ownedApproved(msg.sender,tokenId);
        uint256 gross=uint256(x.priceTicks)*PRICE_TICK; uint256 fee=gross/100;
        if(gross-fee<minNet) revert PriceChanged();
        x.remaining--; if(x.remaining==0) x.status=Status.Filled;
        reservedDAI-=gross+fee;
        uint256 listingId=activeListing[tokenId]; if(listingId!=0) _closeListing(listingId,Status.Invalid);
        emit BidChanged(++mutationCount,id,x.maker,x.status);
        IAwakeCard(TOKEN_A).safeTransferFrom(msg.sender,x.maker,tokenId);
        if(IAwakeCard(TOKEN_A).ownerOf(tokenId)!=x.maker) revert TransferMismatch();
        _moveExact(address(this),msg.sender,gross-fee); _pay(x.maker,fee); _pay(FEE_RECIPIENT,fee);
        _record(true,id,tokenId,msg.sender,x.maker,x.priceTicks,gross,fee);
    }
    function cancelBid(uint256 id) external nonReentrant { Bid storage x=_bid(id); if(x.maker!=msg.sender) revert NotMaker(); _returnBid(id,x,Status.Cancelled); }
    function expireBid(uint256 id) external nonReentrant { Bid storage x=_bid(id); if(block.timestamp<x.expiry) revert NotExpired(); _returnBid(id,x,Status.Expired); }
    function _returnBid(uint256 id,Bid storage x,Status status) private { uint256 refund=_reserve(x.priceTicks,x.remaining); x.status=status; x.remaining=0; reservedDAI-=refund; emit BidChanged(++mutationCount,id,x.maker,status); _pay(x.maker,refund); }
    function _reserve(uint64 price,uint32 cards) private pure returns(uint256){uint256 gross=uint256(price)*PRICE_TICK*cards;return gross+gross/100;}
    function _record(bool isBid,uint256 id,uint256 tokenId,address seller,address buyer,uint64 price,uint256 gross,uint256 fee) private {
        Receipt storage r=isBid?_bidReceipts[id]:_listingReceipts[id]; r.grossFilled+=gross; r.feePaid+=fee; r.filledCards++; r.lastFillBlock=uint64(block.number);
        uint256 tid=++tradeCount; _trades[tid]=Fill(isBid,id,tokenId,seller,buyer,price,gross,fee,uint64(block.number));
        emit Trade(++mutationCount,tid,isBid,id,tokenId,seller,buyer,price,gross,fee);
    }
    function withdraw(address recipient) external nonReentrant { uint256 amount=claimable[recipient]; if(amount==0) revert ZeroAmount(); delete claimable[recipient]; totalClaimable-=amount; _moveExact(address(this),recipient,amount); emit Withdrawn(recipient,amount); }
    function deliver(address recipient,uint256 amount) external { if(msg.sender!=address(this)) revert OnlySelf(); _moveExact(address(this),recipient,amount); }
    function _pay(address recipient,uint256 amount) private {
        if(amount==0) return; if(gasleft()<200000) revert InsufficientGas();
        try this.deliver{gas:150000}(recipient,amount) {} catch { claimable[recipient]+=amount; totalClaimable+=amount; emit PayoutDeferred(recipient,amount); }
    }
    function _moveExact(address from,address to,uint256 amount) private {
        if(amount==0) return;
        uint256 a=INftMarketToken(TOKEN_B).balanceOf(from); uint256 b=INftMarketToken(TOKEN_B).balanceOf(to);
        bytes memory data=from==address(this)?abi.encodeWithSelector(0xa9059cbb,to,amount):abi.encodeWithSelector(0x23b872dd,from,to,amount);
        (bool ok,bytes memory result)=TOKEN_B.call(data);
        if(!ok||(result.length!=0&&!abi.decode(result,(bool)))) revert TransferFailed();
        uint256 aa=INftMarketToken(TOKEN_B).balanceOf(from); uint256 bb=INftMarketToken(TOKEN_B).balanceOf(to);
        if(aa>a||a-aa!=amount||bb<b||bb-b!=amount) revert TransferMismatch();
    }
    function getListing(uint256 id) external view returns(Listing memory){if(_listings[id].maker==address(0))revert NoOrder();return _listings[id];}
    function getBid(uint256 id) external view returns(Bid memory){if(_bids[id].maker==address(0))revert NoOrder();return _bids[id];}
    function getListingReceipt(uint256 id) external view returns(Receipt memory){if(_listings[id].maker==address(0))revert NoOrder();return _listingReceipts[id];}
    function getBidReceipt(uint256 id) external view returns(Receipt memory){if(_bids[id].maker==address(0))revert NoOrder();return _bidReceipts[id];}
    function _page(uint256 cursor,uint256 limit,uint256 count) private pure returns(uint256 start,uint256 size,uint256 next){
        if(limit==0||limit>MAX_PAGE)revert BadPageSize(); start=cursor==0?1:cursor;
        if(start>count)return(start,0,0); size=count-start+1; if(size>limit)size=limit;next=start+size>count?0:start+size;
    }
    function getListings(uint256 cursor,uint256 limit) external view returns(uint256[] memory ids,Listing[] memory rows,uint256 nextCursor){
        (uint256 start,uint256 size,uint256 next)=_page(cursor,limit,listingCount);ids=new uint256[](size);rows=new Listing[](size);
        for(uint256 i=0;i<size;i++){ids[i]=start+i;rows[i]=_listings[start+i];}return(ids,rows,next);
    }
    function getBids(uint256 cursor,uint256 limit) external view returns(uint256[] memory ids,Bid[] memory rows,uint256 nextCursor){
        (uint256 start,uint256 size,uint256 next)=_page(cursor,limit,bidCount);ids=new uint256[](size);rows=new Bid[](size);
        for(uint256 i=0;i<size;i++){ids[i]=start+i;rows[i]=_bids[start+i];}return(ids,rows,next);
    }
    function getTrades(uint256 cursor,uint256 limit) external view returns(uint256[] memory ids,Fill[] memory rows,uint256 nextCursor){
        (uint256 start,uint256 size,uint256 next)=_page(cursor,limit,tradeCount);ids=new uint256[](size);rows=new Fill[](size);
        for(uint256 i=0;i<size;i++){ids[i]=start+i;rows[i]=_trades[start+i];}return(ids,rows,next);
    }
    function getMyOrders(address owner,uint256 cursor,uint256 limit) external view returns(OrderRef[] memory rows,uint256 nextCursor){
        if(limit==0||limit>MAX_PAGE)revert BadPageSize();uint256 count=_myOrders[owner].length;
        if(cursor>=count)return(new OrderRef[](0),0);uint256 size=count-cursor;if(size>limit)size=limit;
        rows=new OrderRef[](size);for(uint256 i=0;i<size;i++)rows[i]=_myOrders[owner][cursor+i];return(rows,cursor+size<count?cursor+size:0);
    }
}
