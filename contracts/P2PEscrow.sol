// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @notice 최소 ERC-20 인터페이스 (balanceOf만 직접 호출, transfer/transferFrom은 low-level call로 처리)
interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
}

/// @title P2PEscrow — 두 토큰(A ⇄ B) 전용 비수탁 P2P 위탁 거래 컨트랙트
/// @notice 설계 원칙
///   1. 관리자·소유자·업그레이드·일시정지 없음. 배포 후 누구도(배포자 포함) 규칙을 바꾸거나 남의 예치금을 건드릴 수 없다.
///   2. 거래 대상 토큰 2종, 수수료율, 수수료 수취 주소는 배포 시 고정(immutable).
///   3. 메이커가 코인을 예치해 호가를 올리고, 테이커가 같은 트랜잭션에서 지불과 수령을 끝낸다(원자적 체결).
///   4. 메이커는 언제든 취소, 만기 지난 호가는 누구나 환불 트리거 가능(환불은 항상 메이커에게만).
///   5. 모든 토큰 이동은 전후 잔액 대조로 검증 → 전송세·비표준 토큰이면 거래가 실패한다(장부 = 실제 잔액).
/// @dev 참고 구현(AilgnsOtcEscrow, 2026-10-30 배포)과 비교한 개선점:
///   - 수수료율이 배포 파라미터(상한 2%)이고 0도 가능 / 수취 주소 0이면 수수료 자체가 없음
///   - 최소 체결 단위(minFill)로 먼지 체결·잔량 먼지 방지
///   - 지정 상대방(taker) 옵션으로 사적 거래 지원
///   - 지불 금액 기준 체결(fillByPay)로 "DAI 100개만큼 사기" 지원
///   - 수령 쪽(메이커·테이커·수수료) 전송도 잔액 대조
///   - 자기 호가 자기 체결 금지, 네이티브 코인 수신 거부, 견적 뷰(quote) 제공
contract P2PEscrow {
    // ───────────────────────── 불변 파라미터 ─────────────────────────
    address public immutable TOKEN_A; // 예: AiLGNS
    address public immutable TOKEN_B; // 예: DAI
    address public immutable FEE_RECIPIENT; // address(0)이면 수수료 없음
    uint256 public immutable FEE_BPS; // 체결 수수료(bp). 테이커가 받는 코인에서 차감

    uint256 public constant MAX_FEE_BPS = 200; // 2% 상한(배포 시 검증)
    uint64 public constant MIN_DURATION = 10 minutes;
    uint64 public constant MAX_DURATION = 30 days;

    // ───────────────────────── 상태 ─────────────────────────
    struct Order {
        address maker;
        address taker; // address(0) = 누구나 체결 가능
        bool sellA; // true: A 예치 → B 수령 / false: B 예치 → A 수령
        bool open;
        uint64 expiry;
        uint256 giveTotal; // 예치 총량(가격 계산 기준)
        uint256 wantTotal; // 원하는 총량
        uint256 giveLeft; // 미체결 잔량
        uint256 minFill; // 최소 체결 단위(give 토큰 기준). 잔량 전부 체결은 항상 허용
    }

    Order[] private _orders;
    uint256 private _lock = 1;

    // ───────────────────────── 이벤트 ─────────────────────────
    event Created(uint256 indexed id, address indexed maker, bool sellA, uint256 give, uint256 want, uint64 expiry, uint256 minFill, address taker);
    event Filled(uint256 indexed id, address indexed taker, uint256 amount, uint256 paid, uint256 fee, uint256 giveLeft);
    event Cancelled(uint256 indexed id, address indexed maker, uint256 refunded);
    event Expired(uint256 indexed id, address indexed maker, uint256 refunded);

    // ───────────────────────── 에러 ─────────────────────────
    error ZeroAddress();
    error SameToken();
    error FeeTooHigh();
    error Reentrancy();
    error NoOrder();
    error Closed();
    error OrderExpired();
    error NotExpired();
    error NotMaker();
    error NotTaker();
    error SelfFill();
    error ZeroAmount();
    error BadDuration();
    error BadMinFill();
    error BelowMinFill();
    error DustLeft();
    error PriceChanged();
    error TransferFailed();
    error TransferMismatch();
    error NativeNotAccepted();

    modifier nonReentrant() {
        if (_lock != 1) revert Reentrancy();
        _lock = 2;
        _;
        _lock = 1;
    }

    constructor(address tokenA, address tokenB, address feeRecipient, uint256 feeBps) {
        if (tokenA == address(0) || tokenB == address(0)) revert ZeroAddress();
        if (tokenA == tokenB) revert SameToken();
        if (feeBps > MAX_FEE_BPS) revert FeeTooHigh();
        TOKEN_A = tokenA;
        TOKEN_B = tokenB;
        FEE_RECIPIENT = feeRecipient;
        FEE_BPS = feeRecipient == address(0) ? 0 : feeBps;
    }

    receive() external payable {
        revert NativeNotAccepted();
    }

    // ───────────────────────── 호가 등록 ─────────────────────────

    /// @param sellA   true면 A를 give만큼 예치하고 B를 want만큼 받고 싶음. false면 반대.
    /// @param give    예치 수량 (가격 = want / give)
    /// @param want    받고 싶은 총량
    /// @param duration 유효 기간(초). 10분 ~ 30일
    /// @param minFill 최소 체결 단위(give 토큰 기준). 0이면 제한 없음. give 이하여야 함
    /// @param taker   지정 상대방. address(0)이면 공개 호가
    function create(bool sellA, uint256 give, uint256 want, uint64 duration, uint256 minFill, address taker)
        external
        nonReentrant
        returns (uint256 id)
    {
        if (give == 0 || want == 0) revert ZeroAmount();
        if (duration < MIN_DURATION || duration > MAX_DURATION) revert BadDuration();
        if (minFill > give) revert BadMinFill();

        _pullExact(sellA ? TOKEN_A : TOKEN_B, msg.sender, address(this), give);

        id = _orders.length;
        uint64 expiry = uint64(block.timestamp) + duration;
        _orders.push(Order(msg.sender, taker, sellA, true, expiry, give, want, give, minFill));
        emit Created(id, msg.sender, sellA, give, want, expiry, minFill, taker);
    }

    // ───────────────────────── 체결 ─────────────────────────

    /// @notice 호가에서 amount만큼 가져가고 그 대가를 메이커에게 지불. 지불·수령은 한 트랜잭션.
    /// @param amount 가져갈 give 토큰 수량(부분 체결 가능)
    /// @param maxPay 지불 상한(가격 재확인용 슬리피지 가드)
    function fill(uint256 id, uint256 amount, uint256 maxPay) external nonReentrant {
        Order storage o = _load(id);
        uint256 pay = _payFor(o, amount);
        if (pay > maxPay) revert PriceChanged();
        _settle(id, o, amount, pay);
    }

    /// @notice 지불할 금액을 기준으로 체결. "want 토큰 pay개만큼만 사겠다"에 해당.
    /// @param pay        지불할 want 토큰 수량
    /// @param minReceive 수수료 차감 후 최소 실수령량(가격 재확인용)
    function fillByPay(uint256 id, uint256 pay, uint256 minReceive) external nonReentrant {
        Order storage o = _load(id);
        if (pay == 0) revert ZeroAmount();
        uint256 amount = (pay * o.giveTotal) / o.wantTotal; // 내림 → 메이커 불리하지 않음
        if (amount > o.giveLeft) amount = o.giveLeft;
        if (amount == 0) revert ZeroAmount();
        uint256 actualPay = _payFor(o, amount); // 실제 지불은 amount 기준 올림 재계산(pay 이하가 보장됨)
        uint256 fee = (amount * FEE_BPS) / 10_000;
        if (amount - fee < minReceive) revert PriceChanged();
        _settle(id, o, amount, actualPay);
    }

    // ───────────────────────── 취소 / 만기 환불 ─────────────────────────

    /// @notice 메이커가 직접 취소. 잔량은 메이커에게 환불.
    function cancel(uint256 id) external nonReentrant {
        if (id >= _orders.length) revert NoOrder();
        Order storage o = _orders[id];
        if (o.maker != msg.sender) revert NotMaker();
        if (!o.open) revert Closed();
        uint256 left = _close(o);
        emit Cancelled(id, o.maker, left);
    }

    /// @notice 만기가 지난 호가를 누구나 환불 처리할 수 있다. 환불은 항상 메이커에게만 간다.
    ///         없는/닫힌/만기 전 호가는 건너뛴다. 전송 실패는 배치 전체를 revert한다.
    ///         토큰 전송이 차단된 호가는 제외하고 개별 id로 재시도해야 한다.
    function refundExpired(uint256[] calldata ids) external nonReentrant {
        for (uint256 i = 0; i < ids.length; i++) {
            uint256 id = ids[i];
            if (id >= _orders.length) continue;
            Order storage o = _orders[id];
            if (!o.open || block.timestamp < o.expiry) continue;
            uint256 left = _close(o);
            emit Expired(id, o.maker, left);
        }
    }

    // ───────────────────────── 조회 ─────────────────────────

    function ordersCount() external view returns (uint256) {
        return _orders.length;
    }

    function getOrder(uint256 id) external view returns (Order memory) {
        if (id >= _orders.length) revert NoOrder();
        return _orders[id];
    }

    /// @notice 번호 구간으로 호가 읽기(프런트 오더북용). 범위를 벗어나면 잘라서 반환.
    function getOrders(uint256 from, uint256 count) external view returns (Order[] memory list) {
        uint256 end = from + count;
        if (end > _orders.length) end = _orders.length;
        if (from >= end) return new Order[](0);
        list = new Order[](end - from);
        for (uint256 i = from; i < end; i++) list[i - from] = _orders[i];
    }

    /// @notice amount만큼 체결할 때 지불액·수수료·실수령액 견적. 체결 가능 여부는 검사하지 않는다.
    function quote(uint256 id, uint256 amount) external view returns (uint256 pay, uint256 fee, uint256 receive_) {
        if (id >= _orders.length) revert NoOrder();
        Order storage o = _orders[id];
        pay = _payFor(o, amount);
        fee = (amount * FEE_BPS) / 10_000;
        receive_ = amount - fee;
    }

    // ───────────────────────── 내부 로직 ─────────────────────────

    function _load(uint256 id) private view returns (Order storage o) {
        if (id >= _orders.length) revert NoOrder();
        o = _orders[id];
        if (!o.open) revert Closed();
        if (block.timestamp >= o.expiry) revert OrderExpired();
        if (o.taker != address(0) && o.taker != msg.sender) revert NotTaker();
        if (o.maker == msg.sender) revert SelfFill();
    }

    /// @dev 지불액 = ceil(amount × want / give). 올림이라 메이커가 손해 보지 않는다.
    function _payFor(Order storage o, uint256 amount) private view returns (uint256) {
        if (amount == 0 || amount > o.giveLeft) revert ZeroAmount();
        return (amount * o.wantTotal + o.giveTotal - 1) / o.giveTotal;
    }

    function _settle(uint256 id, Order storage o, uint256 amount, uint256 pay) private {
        uint256 left = o.giveLeft - amount;
        // 최소 체결 단위: 잔량 전부를 가져가는 경우는 항상 허용. 그 외에는 amount ≥ minFill 이고 남는 잔량도 ≥ minFill 이어야 한다.
        if (left != 0) {
            if (amount < o.minFill) revert BelowMinFill();
            if (left < o.minFill) revert DustLeft();
        }

        // 상태 먼저 갱신(CEI)
        o.giveLeft = left;
        if (left == 0) o.open = false;

        (address giveToken, address wantToken) = o.sellA ? (TOKEN_A, TOKEN_B) : (TOKEN_B, TOKEN_A);

        // 테이커 → 메이커 지불
        _pullExact(wantToken, msg.sender, o.maker, pay);

        // 컨트랙트 → 테이커 수령(수수료 차감), 컨트랙트 → 수수료 수취
        uint256 fee = (amount * FEE_BPS) / 10_000;
        _pushExact(giveToken, msg.sender, amount - fee);
        if (fee > 0) _pushExact(giveToken, FEE_RECIPIENT, fee);

        emit Filled(id, msg.sender, amount, pay, fee, left);
    }

    function _close(Order storage o) private returns (uint256 left) {
        left = o.giveLeft;
        o.open = false;
        o.giveLeft = 0;
        if (left > 0) _pushExact(o.sellA ? TOKEN_A : TOKEN_B, o.maker, left);
    }

    /// @dev 송신자 감소량·수취인 증가량 모두 amount여야 한다(추가 송신세도 거부).
    function _pullExact(address token, address from, address to, uint256 amount) private {
        uint256 senderBefore = IERC20(token).balanceOf(from);
        uint256 before = IERC20(token).balanceOf(to);
        _call(token, abi.encodeWithSelector(0x23b872dd, from, to, amount)); // transferFrom
        uint256 senderAfter = IERC20(token).balanceOf(from);
        uint256 after_ = IERC20(token).balanceOf(to);
        if (senderAfter > senderBefore || senderBefore - senderAfter != amount || after_ < before || after_ - before != amount) revert TransferMismatch();
    }

    /// @dev 에스크로 감소량·수취인 증가량 모두 amount여야 한다.
    function _pushExact(address token, address to, uint256 amount) private {
        if (amount == 0) return;
        uint256 senderBefore = IERC20(token).balanceOf(address(this));
        uint256 before = IERC20(token).balanceOf(to);
        _call(token, abi.encodeWithSelector(0xa9059cbb, to, amount)); // transfer
        uint256 senderAfter = IERC20(token).balanceOf(address(this));
        uint256 after_ = IERC20(token).balanceOf(to);
        if (senderAfter > senderBefore || senderBefore - senderAfter != amount || after_ < before || after_ - before != amount) revert TransferMismatch();
    }

    function _call(address token, bytes memory data) private {
        (bool ok, bytes memory ret) = token.call(data);
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert TransferFailed();
    }
}
