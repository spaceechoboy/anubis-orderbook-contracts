// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// 표준 ERC-20 (테스트용, 누구나 mint)
contract MockERC20 {
    string public name; string public symbol; uint8 public immutable decimals;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
    constructor(string memory n, string memory s, uint8 d) { name = n; symbol = s; decimals = d; }
    function mint(address to, uint256 v) external { totalSupply += v; balanceOf[to] += v; emit Transfer(address(0), to, v); }
    function approve(address sp, uint256 v) external returns (bool) { allowance[msg.sender][sp] = v; emit Approval(msg.sender, sp, v); return true; }
    function transfer(address to, uint256 v) external virtual returns (bool) { return _move(msg.sender, to, v); }
    function transferFrom(address f, address to, uint256 v) external virtual returns (bool) {
        uint256 a = allowance[f][msg.sender];
        require(a >= v, "ALLOWANCE");
        if (a != type(uint256).max) allowance[f][msg.sender] = a - v;
        return _move(f, to, v);
    }
    function _move(address f, address to, uint256 v) internal virtual returns (bool) {
        require(balanceOf[f] >= v, "BALANCE");
        balanceOf[f] -= v; balanceOf[to] += v; emit Transfer(f, to, v); return true;
    }
}

/// 토큰 측 블랙리스트로 특정 수취인의 환불이 실패하는 상황.
contract BlockedReceiverToken is MockERC20 {
    mapping(address => bool) public blocked;
    constructor() MockERC20("Blocked", "BLK", 18) {}
    function setBlocked(address who, bool value) external { blocked[who] = value; }
    function _move(address f, address to, uint256 v) internal override returns (bool) {
        require(!blocked[to], "BLOCKED");
        return super._move(f, to, v);
    }
}

/// 수취량은 정확하지만 송신자에게 1%를 추가 청구한다.
contract SenderTaxToken is MockERC20 {
    bool public taxed;
    constructor() MockERC20("SenderTax", "TAX", 18) {}
    function setTaxed(bool value) external { taxed = value; }
    function _move(address f, address to, uint256 v) internal override returns (bool) {
        super._move(f, to, v);
        uint256 fee = taxed ? v / 100 : 0;
        require(balanceOf[f] >= fee, "TAX_BALANCE");
        balanceOf[f] -= fee;
        balanceOf[address(0xdead)] += fee;
        return true;
    }
}

/// 전송세 1% 토큰 — 에스크로가 거부해야 함
contract FeeOnTransferToken is MockERC20 {
    constructor() MockERC20("FeeTok", "FEE", 18) {}
    function _move(address f, address to, uint256 v) internal override returns (bool) {
        require(balanceOf[f] >= v, "BALANCE");
        uint256 fee = v / 100;
        balanceOf[f] -= v; balanceOf[to] += v - fee; balanceOf[address(0xdead)] += fee;
        emit Transfer(f, to, v - fee); return true;
    }
}

/// 전송 중 에스크로를 재진입하는 악성 토큰
interface IEscrow {
    function cancel(uint256 id) external;
    function fill(uint256 id, uint256 amount, uint256 maxPay) external;
    function refundExpired(uint256[] calldata ids) external;
}
contract ReentrantToken is MockERC20 {
    address public escrow; uint8 public mode; uint256 public targetId; bool public fired; bool public reentrySucceeded; bytes4 public lastErr;
    constructor() MockERC20("Evil", "EVIL", 18) {}
    function arm(address e, uint8 m, uint256 id) external { escrow = e; mode = m; targetId = id; fired = false; }
    function _move(address f, address to, uint256 v) internal override returns (bool) {
        require(balanceOf[f] >= v, "BALANCE");
        balanceOf[f] -= v; balanceOf[to] += v; emit Transfer(f, to, v);
        if (escrow != address(0) && !fired && msg.sender == escrow) {
            fired = true;
            // 재진입 시도. 실패 이유(셀렉터)를 기록해 두고 바깥 전송은 정상 완료시킨다 → 락이 막았는지 테스트에서 확인 가능
            bool ok; bytes memory r;
            if (mode == 1) (ok, r) = escrow.call(abi.encodeWithSelector(IEscrow.cancel.selector, targetId));
            else if (mode == 2) (ok, r) = escrow.call(abi.encodeWithSelector(IEscrow.fill.selector, targetId, uint256(1), type(uint256).max));
            else { uint256[] memory ids = new uint256[](1); ids[0] = targetId; (ok, r) = escrow.call(abi.encodeWithSelector(IEscrow.refundExpired.selector, ids)); }
            reentrySucceeded = ok;
            if (!ok && r.length >= 4) lastErr = bytes4(r);
        }
        return true;
    }
}

/// 토큰 전송 시 콜백을 받아 재진입을 시도하는 "메이커" 컨트랙트 (수령 콜백이 없는 ERC-20에선 불필요하지만 완전성용)
contract Receiver {
    receive() external payable {}
}

/// @dev Local-only authority caller. Production uses a configured multisig.
contract FeeAuthorityHarness {
    address private immutable _owner = msg.sender;

    function forward(address target, bytes calldata data) external {
        require(msg.sender == _owner, "OWNER");
        (bool ok, bytes memory result) = target.call(data);
        if (!ok) {
            assembly { revert(add(result, 32), mload(result)) }
        }
    }
}
