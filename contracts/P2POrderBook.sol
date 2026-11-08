// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

interface IBookToken {
    function balanceOf(address account) external view returns (uint256);
    function decimals() external view returns (uint8);
}

/// @notice A non-upgradeable order book for two 18-decimal tokens, with bounded taker fees in TOKEN_B.
/// @dev Price and size use millionths so the product is always exact in DAI wei.
contract P2POrderBook {
    address public immutable TOKEN_A;
    address public immutable TOKEN_B;
    address public immutable FEE_RECIPIENT;
    address public immutable FEE_AUTHORITY;
    uint16 public immutable MAX_FEE_BPS;
    uint64 public immutable FEE_CHANGE_DELAY;

    uint16 public activeFeeBps;
    uint16 public pendingFeeBps;
    uint64 public feeChangeAfter;
    uint256 public constant LOT_SIZE = 1e12;
    uint256 public constant PRICE_TICK = 1e12;
    uint256 public constant MAX_STEPS = 16;

    enum Status { Open, Filled, Cancelled, Expired, SelfTradePrevented }

    struct Order {
        address maker;
        uint64 expiry;
        bool buy;
        Status status;
        uint64 price;
        uint64 lots;
        uint64 filled;
        uint16 takerFeeBps;
    }

    struct Receipt {
        uint64 placedBlock;
        uint192 quote;
        uint256 fee;
    }

    struct Link {
        uint256 previous;
        uint256 next;
        bool listed;
    }

    uint256 public orderCount;
    uint256 public deferredPayoutCount;
    uint256 public bookMutationCount;
    mapping(address token => mapping(address recipient => uint256 amount)) public claimable;
    mapping(uint256 id => Order) private _orders;
    mapping(uint256 id => Link) private _links;
    mapping(uint256 id => Receipt) private _receipts;
    mapping(address maker => uint256 id) private _latestOrder;
    mapping(uint256 id => uint256 previous) private _previousOrder;
    mapping(bool buy => uint256 id) private _head;
    uint256 private _lock = 1;

    event BookOrderChanged(uint256 indexed sequence, uint256 indexed id, bool listed);
    event OrderPlaced(uint256 indexed id, address indexed maker, bool buy, uint64 price, uint64 lots, uint64 expiry);
    event FeeChangeScheduled(uint16 newBps, uint64 executeAfter);
    event FeeChangeCancelled(uint16 cancelledBps);
    event FeeChangeExecuted(uint16 previousBps, uint16 newBps);
    event Trade(uint256 indexed bidId, uint256 indexed askId, uint64 price, uint64 lots, uint256 quote, uint256 fee);
    event OrderReturned(uint256 indexed id, Status status, uint256 amount);
    event PayoutDeferred(address indexed token, address indexed recipient, uint256 amount);
    event Withdrawn(address indexed token, address indexed recipient, uint256 amount);

    error BadTokens();
    error BadFeeRecipient();
    error BadFeeConfig();
    error OnlyFeeAuthority();
    error FeeTooHigh();
    error FeeChangePending();
    error NoFeeChange();
    error FeeChangeNotReady();
    error FeeChanged();
    error Reentrancy();
    error ZeroAmount();
    error BadDuration();
    error BadPosition();
    error BadPageSize();
    error NoOrder();
    error Closed();
    error NotMaker();
    error NotExpired();
    error BookBusy();
    error SelfTrade();
    error NotEnoughLiquidity();
    error PriceChanged();
    error TransferFailed();
    error TransferMismatch();
    error InsufficientGas();
    error OnlySelf();

    modifier nonReentrant() {
        if (_lock != 1) revert Reentrancy();
        _lock = 2;
        _;
        _lock = 1;
    }

    modifier onlyFeeAuthority() {
        if (FEE_AUTHORITY == address(0) || msg.sender != FEE_AUTHORITY) revert OnlyFeeAuthority();
        _;
    }

    constructor(
        address tokenA,
        address tokenB,
        address feeRecipient,
        address feeAuthority,
        uint16 initialFeeBps,
        uint16 maxFeeBps,
        uint64 feeChangeDelay
    ) {
        if (tokenA == tokenB || tokenA.code.length == 0 || tokenB.code.length == 0) revert BadTokens();
        if (IBookToken(tokenA).decimals() != 18 || IBookToken(tokenB).decimals() != 18) revert BadTokens();
        if (feeRecipient == address(this) || (maxFeeBps != 0 && feeRecipient == address(0))) {
            revert BadFeeRecipient();
        }
        if (initialFeeBps > maxFeeBps || maxFeeBps >= 10_000) revert FeeTooHigh();
        if (feeAuthority == address(0)) {
            if (initialFeeBps != maxFeeBps || feeChangeDelay != 0) revert BadFeeConfig();
        } else if (maxFeeBps == 0 || feeAuthority.code.length == 0 || feeChangeDelay == 0) {
            revert BadFeeConfig();
        }
        TOKEN_A = tokenA;
        TOKEN_B = tokenB;
        FEE_RECIPIENT = feeRecipient;
        FEE_AUTHORITY = feeAuthority;
        MAX_FEE_BPS = maxFeeBps;
        FEE_CHANGE_DELAY = feeChangeDelay;
        activeFeeBps = initialFeeBps;
    }

    function scheduleFeeChange(uint16 newBps) external onlyFeeAuthority nonReentrant {
        if (newBps > MAX_FEE_BPS) revert FeeTooHigh();
        if (feeChangeAfter != 0) revert FeeChangePending();
        uint64 executeAfter = uint64(block.timestamp) + FEE_CHANGE_DELAY;
        pendingFeeBps = newBps;
        feeChangeAfter = executeAfter;
        emit FeeChangeScheduled(newBps, executeAfter);
    }

    function cancelFeeChange() external onlyFeeAuthority nonReentrant {
        if (feeChangeAfter == 0) revert NoFeeChange();
        emit FeeChangeCancelled(pendingFeeBps);
        delete pendingFeeBps;
        delete feeChangeAfter;
    }

    /// @notice Apply the publicly scheduled rate to orders created after this transaction.
    function executeFeeChange() external nonReentrant {
        uint64 executeAfter = feeChangeAfter;
        if (executeAfter == 0) revert NoFeeChange();
        if (block.timestamp < executeAfter) revert FeeChangeNotReady();
        uint16 newBps = pendingFeeBps;
        emit FeeChangeExecuted(activeFeeBps, newBps);
        activeFeeBps = newBps;
        delete pendingFeeBps;
        delete feeChangeAfter;
    }

    /// @param afterId Current preceding order on the same side, or zero to place at the head.
    function placeLimit(bool buy, uint64 price, uint64 lots, uint64 duration, uint256 afterId, uint16 expectedFeeBps)
        external nonReentrant returns (uint256 id)
    {
        if (expectedFeeBps != activeFeeBps) revert FeeChanged();
        if (duration < 10 minutes || duration > 30 days) revert BadDuration();
        id = _create(buy, price, lots, uint64(block.timestamp) + duration);
        _insert(id, afterId);
        _deposit(buy, buy ? _bidReserve(price, lots, expectedFeeBps) : uint256(lots) * LOT_SIZE);
        _advance();
    }

    /// @notice Fill all requested lots in one transaction, or revert every fill.
    /// @param quoteBound Maximum DAI paid or minimum DAI received, with fees included (wei).
    function takeMarket(bool buy, uint64 worstPrice, uint64 lots, uint256 quoteBound, uint16 expectedFeeBps)
        external nonReentrant returns (uint256 id)
    {
        if (expectedFeeBps != activeFeeBps) revert FeeChanged();
        _advance();
        if (needsAdvance()) revert BookBusy();
        id = _create(buy, worstPrice, lots, 0);
        _deposit(buy, buy ? quoteBound : uint256(lots) * LOT_SIZE);

        uint256 total;
        for (uint256 i; i < MAX_STEPS && _orders[id].filled < lots; ++i) {
            uint256 restingId = _head[!buy];
            if (restingId == 0) break;
            Order storage resting = _orders[restingId];
            if (resting.expiry <= block.timestamp) {
                _unlink(restingId);
                continue;
            }
            if (buy ? resting.price > worstPrice : resting.price < worstPrice) break;
            if (resting.maker == msg.sender) revert SelfTrade();
            (uint256 cost, uint256 fee) = buy
                ? _trade(id, restingId, quoteBound - total)
                : _trade(restingId, id, type(uint256).max);
            total += buy ? cost + fee : cost - fee;
        }

        if (_orders[id].filled != lots) revert NotEnoughLiquidity();
        if (!buy && total < quoteBound) revert PriceChanged();
        if (buy && total < quoteBound) _moveExact(TOKEN_B, address(this), msg.sender, quoteBound - total);
    }

    /// @notice Continue matching crossed orders and pruning expired heads, up to MAX_STEPS.
    function advance() external nonReentrant { _advance(); }

    function needsAdvance() public view returns (bool) {
        Order storage bid = _orders[_head[true]];
        Order storage ask = _orders[_head[false]];
        return (bid.maker != address(0) && bid.expiry <= block.timestamp)
            || (ask.maker != address(0) && ask.expiry <= block.timestamp)
            || (ask.maker != address(0) && bid.price >= ask.price);
    }

    function cancel(uint256 id) external nonReentrant {
        Order storage order = _open(id);
        if (order.maker != msg.sender) revert NotMaker();
        _return(id, order, Status.Cancelled);
    }

    function refundExpired(uint256 id) external nonReentrant {
        Order storage order = _open(id);
        if (block.timestamp < order.expiry) revert NotExpired();
        _return(id, order, Status.Expired);
    }

    /// @notice Retry a failed payout. Funds always go to the recorded recipient.
    function withdraw(address token, address recipient) external nonReentrant {
        uint256 amount = claimable[token][recipient];
        if (amount == 0) revert ZeroAmount();
        delete claimable[token][recipient];
        _moveExact(token, address(this), recipient, amount);
        emit Withdrawn(token, recipient, amount);
    }

    /// @dev An isolated transfer lets a recipient-specific failure leave a credit instead of blocking the book.
    function deliver(address token, address recipient, uint256 amount) external {
        if (msg.sender != address(this)) revert OnlySelf();
        _moveExact(token, address(this), recipient, amount);
    }

    function getOrder(uint256 id) external view returns (Order memory) {
        if (_orders[id].maker == address(0)) revert NoOrder();
        return _orders[id];
    }

    function getOrderReceipt(uint256 id)
        external view returns (Order memory order, uint256 quote, uint256 fee, uint64 placedBlock)
    {
        order = _orders[id];
        if (order.maker == address(0)) revert NoOrder();
        Receipt storage receipt = _receipts[id];
        return (order, receipt.quote, receipt.fee, receipt.placedBlock);
    }

    function getRecentOrders(address maker, uint256 limit)
        external view returns (uint256[] memory ids, Order[] memory rows)
    {
        if (limit == 0 || limit > 64) revert BadPageSize();
        uint256 first = _latestOrder[maker];
        uint256 count;
        for (uint256 id = first; id != 0 && count < limit; id = _previousOrder[id]) ++count;
        ids = new uint256[](count);
        rows = new Order[](count);
        for (uint256 i; i < count; ++i) {
            ids[i] = first;
            rows[i] = _orders[first];
            first = _previousOrder[first];
        }
    }

    /// @notice Page through all orders, including expired orders removed from the price book.
    function getOrdersById(uint256 first, uint256 limit) external view returns (Order[] memory rows) {
        if (limit == 0 || limit > 64) revert BadPageSize();
        if (first == 0) revert NoOrder();
        if (first > orderCount) return new Order[](0);
        uint256 remaining = orderCount - first + 1;
        uint256 count = remaining < limit ? remaining : limit;
        rows = new Order[](count);
        for (uint256 i; i < count; ++i) rows[i] = _orders[first + i];
    }

    /// @notice Page through price-time order at a fixed block. Expired rows may await pruning.
    function getBook(bool buy, uint256 afterId, uint256 limit)
        external view returns (uint256[] memory ids, Order[] memory rows)
    {
        if (limit == 0 || limit > 64) revert BadPageSize();
        if (afterId != 0 && (!_links[afterId].listed || _orders[afterId].buy != buy)) revert BadPosition();
        uint256 first = afterId == 0 ? _head[buy] : _links[afterId].next;
        uint256 count;
        for (uint256 id = first; id != 0 && count < limit; id = _links[id].next) ++count;
        ids = new uint256[](count);
        rows = new Order[](count);
        for (uint256 i; i < count; ++i) {
            ids[i] = first;
            rows[i] = _orders[first];
            first = _links[first].next;
        }
    }

    function _create(bool buy, uint64 price, uint64 lots, uint64 expiry) private returns (uint256 id) {
        if (price == 0 || lots == 0) revert ZeroAmount();
        id = ++orderCount;
        _orders[id] = Order(msg.sender, expiry, buy, Status.Open, price, lots, 0, activeFeeBps);
        _receipts[id].placedBlock = uint64(block.number);
        _previousOrder[id] = _latestOrder[msg.sender];
        _latestOrder[msg.sender] = id;
        emit OrderPlaced(id, msg.sender, buy, price, lots, expiry);
    }

    function _open(uint256 id) private view returns (Order storage order) {
        order = _orders[id];
        if (order.maker == address(0)) revert NoOrder();
        if (order.status != Status.Open) revert Closed();
    }

    function _before(uint256 first, uint256 second, bool buy) private view returns (bool) {
        uint64 a = _orders[first].price;
        uint64 b = _orders[second].price;
        if (a == b) return first < second;
        return buy ? a > b : a < b;
    }

    function _insert(uint256 id, uint256 afterId) private {
        bool buy = _orders[id].buy;
        if (afterId != 0 && (!_links[afterId].listed || _orders[afterId].buy != buy || !_before(afterId, id, buy))) {
            revert BadPosition();
        }
        uint256 next = afterId == 0 ? _head[buy] : _links[afterId].next;
        if (next != 0 && !_before(id, next, buy)) revert BadPosition();
        _links[id] = Link(afterId, next, true);
        if (afterId == 0) _head[buy] = id;
        else _links[afterId].next = id;
        if (next != 0) _links[next].previous = id;
        emit BookOrderChanged(++bookMutationCount, id, true);
    }

    function _unlink(uint256 id) private {
        Link memory link = _links[id];
        if (!link.listed) return;
        if (link.previous == 0) _head[_orders[id].buy] = link.next;
        else _links[link.previous].next = link.next;
        if (link.next != 0) _links[link.next].previous = link.previous;
        delete _links[id];
        emit BookOrderChanged(++bookMutationCount, id, false);
    }

    function _advance() private {
        for (uint256 i; i < MAX_STEPS; ++i) {
            uint256 bidId = _head[true];
            uint256 askId = _head[false];
            Order storage bid = _orders[bidId];
            Order storage ask = _orders[askId];
            if (bidId != 0 && bid.expiry <= block.timestamp) _unlink(bidId);
            else if (askId != 0 && ask.expiry <= block.timestamp) _unlink(askId);
            else if (bidId == 0 || askId == 0 || bid.price < ask.price) break;
            else if (bid.maker != ask.maker) _trade(bidId, askId, type(uint256).max);
            else if (bidId > askId) _return(bidId, bid, Status.SelfTradePrevented);
            else _return(askId, ask, Status.SelfTradePrevented);
        }
    }

    function _trade(uint256 bidId, uint256 askId, uint256 maxBuyerDebit) private returns (uint256 cost, uint256 fee) {
        Order storage bid = _orders[bidId];
        Order storage ask = _orders[askId];
        uint64 lots = bid.lots - bid.filled;
        uint64 askLeft = ask.lots - ask.filled;
        if (askLeft < lots) lots = askLeft;
        uint64 price = bidId < askId ? bid.price : ask.price;
        cost = _quote(price, lots);
        bool buyerTakes = bidId > askId;
        fee = _fee(cost, buyerTakes ? bid.takerFeeBps : ask.takerFeeBps);
        if (buyerTakes && cost + fee > maxBuyerDebit) revert PriceChanged();
        _recordFill(bidId, bid, lots, cost, buyerTakes ? fee : 0);
        _recordFill(askId, ask, lots, cost, buyerTakes ? 0 : fee);
        _payOrder(bid, TOKEN_A, uint256(lots) * LOT_SIZE);
        _payOrder(ask, TOKEN_B, cost - (buyerTakes ? 0 : fee));
        if (bid.expiry != 0) {
            uint256 spent = cost + (buyerTakes ? fee : 0);
            _pay(TOKEN_B, bid.maker, _bidReserve(bid.price, lots, bid.takerFeeBps) - spent);
        }
        _pay(TOKEN_B, FEE_RECIPIENT, fee);
        emit Trade(bidId, askId, price, lots, cost, fee);
    }

    function _payOrder(Order storage order, address token, uint256 amount) private {
        // The market submitter must receive the quoted token in their wallet in this transaction.
        if (order.expiry == 0) _moveExact(token, address(this), order.maker, amount);
        else _pay(token, order.maker, amount);
    }

    function _recordFill(uint256 id, Order storage order, uint64 lots, uint256 cost, uint256 fee) private {
        order.filled += lots;
        Receipt storage receipt = _receipts[id];
        // Total quote is bounded by uint64 price × uint64 lots × 1e6.
        receipt.quote += uint192(cost);
        if (fee != 0) receipt.fee += fee;
        if (order.lots == order.filled) {
            order.status = Status.Filled;
            _unlink(id);
        } else if (_links[id].listed) {
            emit BookOrderChanged(++bookMutationCount, id, true);
        }
    }

    function _return(uint256 id, Order storage order, Status status) private {
        uint64 left = order.lots - order.filled;
        order.status = status;
        _unlink(id);
        uint256 amount = order.buy ? _bidReserve(order.price, left, order.takerFeeBps) : uint256(left) * LOT_SIZE;
        _pay(order.buy ? TOKEN_B : TOKEN_A, order.maker, amount);
        emit OrderReturned(id, status, amount);
    }

    function _quote(uint64 price, uint64 lots) private pure returns (uint256) {
        return uint256(price) * lots * (PRICE_TICK * LOT_SIZE / 1e18);
    }

    function _fee(uint256 cost, uint16 bps) private pure returns (uint256) {
        return cost * bps / 10_000;
    }

    function _bidReserve(uint64 price, uint64 lots, uint16 bps) private pure returns (uint256) {
        // Quote granularity keeps the fee additive across partial fills.
        uint256 cost = _quote(price, lots);
        return cost + _fee(cost, bps);
    }

    function _deposit(bool buy, uint256 amount) private {
        if (amount == 0) revert ZeroAmount();
        _moveExact(buy ? TOKEN_B : TOKEN_A, msg.sender, address(this), amount);
    }

    function _pay(address token, address recipient, uint256 amount) private {
        if (amount == 0) return;
        // The caller's gas limit must leave the full delivery budget and room to record a credit.
        if (gasleft() < 200_000) revert InsufficientGas();
        try this.deliver{gas: 150_000}(token, recipient, amount) {} catch {
            claimable[token][recipient] += amount;
            ++deferredPayoutCount;
            emit PayoutDeferred(token, recipient, amount);
        }
    }

    function _moveExact(address token, address from, address to, uint256 amount) private {
        uint256 fromBefore = IBookToken(token).balanceOf(from);
        uint256 toBefore = IBookToken(token).balanceOf(to);
        bytes memory data = from == address(this)
            ? abi.encodeWithSelector(0xa9059cbb, to, amount)
            : abi.encodeWithSelector(0x23b872dd, from, to, amount);
        (bool ok, bytes memory result) = token.call(data);
        if (!ok || (result.length != 0 && !abi.decode(result, (bool)))) revert TransferFailed();
        uint256 fromAfter = IBookToken(token).balanceOf(from);
        uint256 toAfter = IBookToken(token).balanceOf(to);
        if (fromAfter > fromBefore || fromBefore - fromAfter != amount || toAfter < toBefore || toAfter - toBefore != amount) {
            revert TransferMismatch();
        }
    }
}
