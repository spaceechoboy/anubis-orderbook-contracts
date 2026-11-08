// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

interface ISettlementERC20 {
    function decimals() external view returns (uint8);
    function balanceOf(address who) external view returns (uint256);
    function transferFrom(address from,address to,uint256 amount) external returns (bool);
}
interface ISettlementERC721 {
    function supportsInterface(bytes4 interfaceId) external view returns (bool);
    function balanceOf(address who) external view returns (uint256);
    function ownerOf(uint256 tokenId) external view returns (address);
    function safeTransferFrom(address from,address to,uint256 tokenId) external;
}

/// @notice Immutable, market-specific settlement for signed, single-counterparty invitations.
/// @dev No deposit, withdrawal queue, administrator, arbitrary call or upgrade path.
///      Only ECDSA signatures are supported; ERC-1271 smart-account signing is not implemented.
///      Ordinary EOAs (including delegated EOAs) do not need to become smart accounts.
contract NoncustodialSettlement {
    uint16 public constant FEE_BPS = 100;
    uint256 public constant MAX_ORDER_LIFETIME = 86400;
    uint256 public constant MAX_PERMIT_LIFETIME = 300;
    address public immutable BASE_TOKEN;
    address public immutable QUOTE_TOKEN;
    address public immutable FEE_RECIPIENT;
    address public immutable GATE_SIGNER;
    uint8 public immutable BASE_DECIMALS;
    uint8 public immutable ASSET_KIND; // 0 = ERC20, 1 = ERC721
    uint256 public immutable LOT_SIZE;
    bytes32 public immutable MARKET_ID;
    bytes32 public immutable DEPLOYMENT_ID;
    bytes32 private constant DOMAIN_TYPEHASH = keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    bytes32 private constant NAME_HASH = keccak256("Anubis Invite Settlement");
    bytes32 private constant VERSION_HASH = keccak256("1");
    bytes32 public constant ORDER_TYPEHASH = keccak256("Order(address maker,uint256 makerNonce,uint256 makerEpoch,bytes32 marketId,uint8 assetKind,address baseToken,address quoteToken,uint8 baseDecimals,bool buy,uint64 priceTicks,uint64 totalLots,uint64 minFillLots,bool allowPartial,uint8 nftMode,uint256 tokenId,uint64 validAfter,uint64 expiresAt,uint16 feeBps,address feeRecipient,address gateSigner,bytes32 inviteDigest,bool singleCounterparty,bytes32 deploymentId)");
    bytes32 public constant PERMIT_TYPEHASH = keccak256("GatePermit(bytes32 orderHash,address taker,uint64 fillLots,uint64 expectedFilledLots,uint256 permitNonce,uint64 issuedAt,uint64 expiresAt,uint256 chainId,address verifyingContract,uint256 tokenId)");
    uint256 private constant HALF_ORDER = 0x7fffffffffffffffffffffffffffffff5d576e7357a4501ddfe92f46681b20a0;

    struct Order {
        address maker; uint256 makerNonce; uint256 makerEpoch;
        bytes32 marketId; uint8 assetKind; address baseToken; address quoteToken; uint8 baseDecimals;
        bool buy; uint64 priceTicks; uint64 totalLots; uint64 minFillLots; bool allowPartial;
        uint8 nftMode; uint256 tokenId; uint64 validAfter; uint64 expiresAt;
        uint16 feeBps; address feeRecipient; address gateSigner; bytes32 inviteDigest;
        bool singleCounterparty; bytes32 deploymentId;
    }
    struct GatePermit {
        bytes32 orderHash; address taker; uint64 fillLots; uint64 expectedFilledLots;
        uint256 permitNonce; uint64 issuedAt; uint64 expiresAt;
        uint256 chainId; address verifyingContract; uint256 tokenId;
    }
    struct Execution {
        uint64 cumulative; uint256 gross; uint256 fee; uint256 debit; uint256 credit;
    }
    struct Balances {
        address[4] parties;
        uint256[4] baseBefore;
        uint256[4] quoteBefore;
        uint256[4] baseCredit;
        uint256[4] baseDebit;
        uint256[4] quoteCredit;
        uint256[4] quoteDebit;
    }
    mapping(bytes32 => uint64) public filledLots;
    mapping(bytes32 => bool) public cancelled;
    mapping(address => mapping(uint256 => bool)) public cancelledNonce;
    mapping(address => mapping(uint256 => bytes32)) public nonceToOrderHash;
    mapping(address => uint256) public epoch;
    mapping(bytes32 => address) public boundTaker;
    mapping(bytes32 => bool) public usedPermit;
    uint256 private _entered;

    error BadConfig(); error BadOrder(); error Closed(); error BadPermit(); error BadSignature();
    error BadFill(); error NotMaker(); error BadEpoch(); error NonceUsed(); error WrongTaker();
    error PriceChanged(); error TransferFailed(); error TransferMismatch(); error Reentrant();
    event Filled(bytes32 indexed orderHash,address indexed maker,address indexed taker,uint64 fillLots,uint64 cumulativeLots,uint256 tokenId,uint256 gross,uint256 fee,uint256 takerDaiDebit,uint256 takerDaiCredit,bytes32 permitDigest);
    event Cancelled(bytes32 indexed orderHash,address indexed maker);
    event NonceCancelled(address indexed maker,uint256 nonce);
    event EpochAdvanced(address indexed maker,uint256 newEpoch);

    modifier nonReentrant() {
        if (_entered != 0) revert Reentrant();
        _entered = 1; _; _entered = 0;
    }

    constructor(address base,address quote,uint8 precision,uint8 kind,address recipient,bytes32 deploymentId,address gateSigner) {
        if (base == quote || base.code.length == 0 || quote.code.length == 0 || kind > 1 ||
            recipient == address(0) || recipient == address(this) || recipient == base || recipient == quote ||
            deploymentId == bytes32(0) || gateSigner == address(0) || gateSigner == address(this) ||
            gateSigner == base || gateSigner == quote) revert BadConfig();
        if (ISettlementERC20(quote).decimals() != 18) revert BadConfig();
        if (kind == 0) {
            if ((precision != 9 && precision != 18) || ISettlementERC20(base).decimals() != precision) revert BadConfig();
        } else {
            if (precision != 0 || !ISettlementERC721(base).supportsInterface(0x80ac58cd)) revert BadConfig();
        }
        BASE_TOKEN = base; QUOTE_TOKEN = quote; BASE_DECIMALS = precision; ASSET_KIND = kind;
        FEE_RECIPIENT = recipient; DEPLOYMENT_ID = deploymentId; GATE_SIGNER = gateSigner;
        LOT_SIZE = kind == 0 ? 10 ** uint256(precision) / 1e6 : 1;
        MARKET_ID = keccak256(abi.encode(base,quote,precision,kind));
    }

    function domainSeparator() public view returns (bytes32) {
        return keccak256(abi.encode(DOMAIN_TYPEHASH,NAME_HASH,VERSION_HASH,block.chainid,address(this)));
    }
    function hashOrder(Order calldata order) public view returns (bytes32) {
        return _typedHash(keccak256(abi.encode(ORDER_TYPEHASH,order)));
    }
    function hashPermit(GatePermit calldata permit) public view returns (bytes32) {
        return _typedHash(keccak256(abi.encode(PERMIT_TYPEHASH,permit)));
    }
    function _typedHash(bytes32 structHash) private view returns (bytes32) {
        return keccak256(abi.encodePacked(hex"1901",domainSeparator(),structHash));
    }
    function cancel(Order calldata order) external nonReentrant {
        if (order.maker != msg.sender) revert NotMaker();
        bytes32 hash = hashOrder(order); cancelled[hash] = true;
        emit Cancelled(hash,msg.sender);
    }
    function cancelNonce(uint256 nonce) external nonReentrant {
        cancelledNonce[msg.sender][nonce] = true; emit NonceCancelled(msg.sender,nonce);
    }
    function cancelAll(uint256 newEpoch) external nonReentrant {
        if (newEpoch <= epoch[msg.sender]) revert BadEpoch();
        epoch[msg.sender] = newEpoch; emit EpochAdvanced(msg.sender,newEpoch);
    }

    /// @param maxDebit Maximum actual DAI debit of the taker on a maker-sell order.
    /// @param minNet Minimum actual DAI credit of the taker on a maker-buy order.
    /// @dev Fee-recipient/taker alias is netted in these limits; gas is excluded.
    function fill(Order calldata order,bytes calldata makerSignature,GatePermit calldata permit,bytes calldata gateSignature,uint256 maxDebit,uint256 minNet) external nonReentrant {
        bytes32 oh = hashOrder(order);
        _validateOrder(order,oh);
        if (_recover(oh,makerSignature) != order.maker) revert BadSignature();
        bytes32 ph = hashPermit(permit);
        _validatePermit(order,permit,oh,ph);
        if (_recover(ph,gateSignature) != order.gateSigner) revert BadSignature();
        _execute(order,permit,oh,ph,maxDebit,minNet);
    }

    function _execute(Order calldata order,GatePermit calldata permit,bytes32 oh,bytes32 ph,uint256 maxDebit,uint256 minNet) private {
        Execution memory x;
        {
            uint64 previous = filledLots[oh];
            uint256 cumulative = uint256(previous) + permit.fillLots;
            if (permit.fillLots == 0 || cumulative > order.totalLots) revert BadFill();
            uint256 remaining = uint256(order.totalLots) - cumulative;
            if ((!order.allowPartial && permit.fillLots != order.totalLots) ||
                (permit.fillLots < order.minFillLots && remaining != 0) ||
                (remaining != 0 && remaining < order.minFillLots)) revert BadFill();
            _validateCard(order,permit);
            uint256 multiplier = ASSET_KIND == 0 ? 1e6 : 1e12;
            uint256 oldGross = uint256(previous) * order.priceTicks * multiplier;
            uint256 newGross = cumulative * order.priceTicks * multiplier;
            x.cumulative = uint64(cumulative);
            x.gross = newGross-oldGross; x.fee = newGross/100-oldGross/100;
        }
        x.debit = order.buy ? 0 : x.gross + (permit.taker == FEE_RECIPIENT ? 0 : x.fee);
        x.credit = order.buy ? x.gross - (permit.taker == FEE_RECIPIENT ? 0 : x.fee) : 0;
        if ((!order.buy && x.debit > maxDebit) || (order.buy && x.credit < minNet)) revert PriceChanged();
        // Effects precede every token call. A failure in any subsequent leg reverts all effects.
        filledLots[oh] = x.cumulative; usedPermit[ph] = true;
        nonceToOrderHash[order.maker][order.makerNonce] = oh; boundTaker[oh] = permit.taker;
        _settle(order,permit,x.gross,x.fee);
        emit Filled(oh,order.maker,permit.taker,permit.fillLots,x.cumulative,permit.tokenId,x.gross,x.fee,x.debit,x.credit,ph);
    }

    function _validateOrder(Order calldata o,bytes32 h) private view {
        if (!_party(o.maker) || o.marketId != MARKET_ID || o.assetKind != ASSET_KIND || o.baseToken != BASE_TOKEN ||
            o.quoteToken != QUOTE_TOKEN || o.baseDecimals != BASE_DECIMALS || o.feeBps != FEE_BPS ||
            o.feeRecipient != FEE_RECIPIENT || o.deploymentId != DEPLOYMENT_ID || o.gateSigner != GATE_SIGNER ||
            o.inviteDigest == bytes32(0) || !o.singleCounterparty || o.priceTicks == 0 || o.totalLots == 0 ||
            o.minFillLots == 0 || o.minFillLots > o.totalLots) revert BadOrder();
        if (o.expiresAt <= o.validAfter || uint256(o.expiresAt) - o.validAfter > MAX_ORDER_LIFETIME) revert BadOrder();
        if (block.timestamp < o.validAfter || block.timestamp >= o.expiresAt || cancelled[h] ||
            cancelledNonce[o.maker][o.makerNonce] || o.makerEpoch != epoch[o.maker] || filledLots[h] == o.totalLots) revert Closed();
        bytes32 assigned = nonceToOrderHash[o.maker][o.makerNonce];
        if (assigned != bytes32(0) && assigned != h) revert NonceUsed();
    }
    function _validatePermit(Order calldata o,GatePermit calldata p,bytes32 oh,bytes32 ph) private view {
        if (p.orderHash != oh || p.chainId != block.chainid || p.verifyingContract != address(this) ||
            p.expectedFilledLots != filledLots[oh] || p.expiresAt <= p.issuedAt ||
            uint256(p.expiresAt) - p.issuedAt > MAX_PERMIT_LIFETIME || p.expiresAt > o.expiresAt ||
            p.issuedAt < o.validAfter || block.timestamp < p.issuedAt || block.timestamp >= p.expiresAt || usedPermit[ph]) revert BadPermit();
        if (!_party(p.taker) || msg.sender != p.taker || p.taker == o.maker ||
            (boundTaker[oh] != address(0) && boundTaker[oh] != p.taker)) revert WrongTaker();
    }
    function _validateCard(Order calldata o,GatePermit calldata p) private pure {
        if (o.assetKind == 0) {
            if (o.nftMode != 0 || o.tokenId != 0 || p.tokenId != 0) revert BadOrder();
        } else {
            if (p.fillLots != 1 || o.minFillLots != 1) revert BadFill();
            if (o.nftMode == 1) {
                if (o.totalLots != 1 || o.allowPartial || p.tokenId != o.tokenId) revert BadOrder();
            } else if (o.nftMode == 2) {
                if (!o.buy || o.tokenId != 0 || (o.totalLots > 1 && !o.allowPartial)) revert BadOrder();
            } else revert BadOrder();
        }
    }
    function _party(address who) private view returns (bool) {
        return who != address(0) && who != address(this) && who != BASE_TOKEN && who != QUOTE_TOKEN;
    }

    function _settle(Order calldata o,GatePermit calldata p,uint256 gross,uint256 fee) private {
        Balances memory b;
        b.parties = [o.maker,p.taker,FEE_RECIPIENT,address(this)];
        for (uint256 i; i < 4; ++i) {
            b.baseBefore[i] = ISettlementERC20(BASE_TOKEN).balanceOf(b.parties[i]);
            b.quoteBefore[i] = ISettlementERC20(QUOTE_TOKEN).balanceOf(b.parties[i]);
        }
        address baseFrom = o.buy ? p.taker : o.maker;
        address baseTo = o.buy ? o.maker : p.taker;
        uint256 baseAmount = uint256(p.fillLots) * LOT_SIZE;
        _plan(b.parties,b.baseCredit,b.baseDebit,baseFrom,baseTo,baseAmount);
        address payer = o.buy ? o.maker : p.taker;
        address payee = o.buy ? p.taker : o.maker;
        uint256 payeeAmount = o.buy ? gross-fee : gross;
        _plan(b.parties,b.quoteCredit,b.quoteDebit,payer,payee,payeeAmount);
        _plan(b.parties,b.quoteCredit,b.quoteDebit,payer,FEE_RECIPIENT,fee);
        if (ASSET_KIND == 0) {
            _transfer(BASE_TOKEN,baseFrom,baseTo,baseAmount);
        } else {
            if (ISettlementERC721(BASE_TOKEN).ownerOf(p.tokenId) != baseFrom) revert TransferMismatch();
            ISettlementERC721(BASE_TOKEN).safeTransferFrom(baseFrom,baseTo,p.tokenId);
        }
        _transfer(QUOTE_TOKEN,payer,payee,payeeAmount);
        _transfer(QUOTE_TOKEN,payer,FEE_RECIPIENT,fee);
        // Inspect both assets only AFTER all transfers/callbacks, including final fee transfer.
        if (ASSET_KIND == 1 && ISettlementERC721(BASE_TOKEN).ownerOf(p.tokenId) != baseTo) revert TransferMismatch();
        _verify(BASE_TOKEN,b.parties,b.baseBefore,b.baseCredit,b.baseDebit);
        _verify(QUOTE_TOKEN,b.parties,b.quoteBefore,b.quoteCredit,b.quoteDebit);
    }
    function _plan(address[4] memory parties,uint256[4] memory credit,uint256[4] memory debit,address from,address to,uint256 amount) private pure {
        if (from == to) return;
        for (uint256 i; i < 4; ++i) {
            if (parties[i] == from) debit[i] += amount;
            if (parties[i] == to) credit[i] += amount;
        }
    }
    function _verify(address token,address[4] memory parties,uint256[4] memory beforeBalances,uint256[4] memory credit,uint256[4] memory debit) private view {
        for (uint256 i; i < 4; ++i) {
            bool duplicate;
            for (uint256 j; j < i; ++j) if (parties[i] == parties[j]) duplicate = true;
            if (duplicate) continue;
            // Subtraction prevents balance+credit overflow; it also validates net alias cases.
            uint256 afterBalance = ISettlementERC20(token).balanceOf(parties[i]);
            if (credit[i] >= debit[i]) {
                if (afterBalance < beforeBalances[i] || afterBalance-beforeBalances[i] != credit[i]-debit[i]) revert TransferMismatch();
            } else {
                if (beforeBalances[i] < afterBalance || beforeBalances[i]-afterBalance != debit[i]-credit[i]) revert TransferMismatch();
            }
        }
    }
    function _transfer(address token,address from,address to,uint256 amount) private {
        if (from == to || amount == 0) return;
        (bool ok,bytes memory result) = token.call(abi.encodeWithSelector(ISettlementERC20.transferFrom.selector,from,to,amount));
        if (!ok || (result.length != 0 && (result.length != 32 || abi.decode(result,(uint256)) != 1))) revert TransferFailed();
    }
    function _recover(bytes32 digest,bytes calldata signature) private pure returns (address signer) {
        if (signature.length != 65) revert BadSignature();
        bytes32 r; bytes32 s; uint8 v;
        assembly {
            r := calldataload(signature.offset)
            s := calldataload(add(signature.offset,32))
            v := byte(0,calldataload(add(signature.offset,64)))
        }
        if (uint256(s) > HALF_ORDER || uint256(s) == 0 || (v != 27 && v != 28)) revert BadSignature();
        signer = ecrecover(digest,v,r,s);
        if (signer == address(0)) revert BadSignature();
    }
}
