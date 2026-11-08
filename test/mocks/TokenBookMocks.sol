// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "./Mocks.sol";

/// @dev Test-only token with configurable precision, upgrade-like restrictions/taxes and callbacks.
contract TokenBookMock is MockERC20 {
    mapping(address => bool) public blocked;
    uint16 public senderTax;
    uint16 public receiverTax;
    address public callback;
    bytes public callbackData;
    bool public fired;
    bool public reentrySucceeded;
    bytes4 public lastErr;

    constructor(uint8 precision) MockERC20("TokenBookMock", "TBM", precision) {}

    function setBlocked(address who, bool value) external { blocked[who] = value; }
    function setTax(uint16 sender, uint16 receiver) external {
        require(sender <= 10_000 && receiver <= 10_000, "TAX");
        senderTax = sender;
        receiverTax = receiver;
    }
    function arm(address target, bytes calldata data) external {
        callback = target;
        callbackData = data;
        fired = false;
        reentrySucceeded = false;
        lastErr = bytes4(0);
    }
    function _move(address from, address to, uint256 amount) internal override returns (bool) {
        require(!blocked[from] && !blocked[to], "BLOCKED");
        uint256 extra = amount * senderTax / 10_000;
        uint256 withheld = amount * receiverTax / 10_000;
        require(balanceOf[from] >= amount + extra, "BALANCE");
        balanceOf[from] -= amount + extra;
        balanceOf[to] += amount - withheld;
        balanceOf[address(0xdead)] += extra + withheld;
        emit Transfer(from, to, amount - withheld);
        if (callback != address(0) && !fired && msg.sender == callback) {
            fired = true;
            (bool ok, bytes memory result) = callback.call(callbackData);
            reentrySucceeded = ok;
            if (!ok && result.length >= 4) lastErr = bytes4(result);
        }
        return true;
    }
}
