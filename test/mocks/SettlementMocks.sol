// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;
import "./TokenBookMocks.sol";

/// @dev Test token returning nonstandard responses after actual state changes.
contract SettlementToken is TokenBookMock {
    uint8 public mode;
    address public hookTarget;
    bytes public hookData;
    uint256 public hookAt;
    uint256 public calls;
    constructor(uint8 precision) TokenBookMock(precision) {}
    function setMode(uint8 value) external { mode = value; }
    function setHook(address target,bytes calldata data,uint256 at) external {
        hookTarget=target;hookData=data;hookAt=at;calls=0;
    }
    function transferFrom(address from,address to,uint256 amount) public override returns (bool) {
        if (mode == 5) return true;
        uint256 allowed = allowance[from][msg.sender];
        require(allowed >= amount,"ALLOWANCE");
        if(allowed != type(uint256).max) allowance[from][msg.sender] = allowed-amount;
        bool moved = _move(from,to,amount);
        ++calls;
        if(hookTarget != address(0) && calls == hookAt){
            (bool ok,) = hookTarget.call(hookData);require(ok,"HOOK");
        }
        if (mode == 1) return false;
        if (mode == 2) { assembly { return(0,0) } }
        if (mode == 3) { assembly { mstore(0,1) return(31,1) } }
        if (mode == 4) revert("TRANSFER_REVERT");
        if (mode == 6) { assembly { mstore(0,2) return(0,32) } }
        return moved;
    }
}
