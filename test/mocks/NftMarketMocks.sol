// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

interface ITestNftReceiver {
    function onERC721Received(address operator,address from,uint256 tokenId,bytes calldata data) external returns(bytes4);
}

/// @dev Test ERC721 with per-card approvals, operator approvals and reversible transfer restrictions.
contract NftMarketMock {
    mapping(uint256=>address) private _owners;
    mapping(address=>uint256) public balanceOf;
    mapping(uint256=>address) private _approved;
    mapping(address=>mapping(address=>bool)) public isApprovedForAll;
    mapping(uint256=>bool) public locked;
    bool public ownerReadReverts;
    bool public approvalReadReverts;
    bool public interfaceSupported=true;
    event Transfer(address indexed from,address indexed to,uint256 indexed tokenId);
    event Approval(address indexed owner,address indexed approved,uint256 indexed tokenId);
    event ApprovalForAll(address indexed owner,address indexed operator,bool approved);
    function supportsInterface(bytes4 id) external view returns(bool){return interfaceSupported&&(id==0x80ac58cd||id==0x01ffc9a7);}
    function setInterfaceSupported(bool value) external {interfaceSupported=value;}
    function setReadFailures(bool ownerFailure,bool approvalFailure) external {ownerReadReverts=ownerFailure;approvalReadReverts=approvalFailure;}
    function ownerOf(uint256 id) public view returns(address){require(!ownerReadReverts,"OWNER_READ_UNAVAILABLE");address owner=_owners[id];require(owner!=address(0),"MISSING");return owner;}
    function getApproved(uint256 id) external view returns(address){require(!approvalReadReverts,"APPROVAL_READ_UNAVAILABLE");ownerOf(id);return _approved[id];}
    function mintMany(address to,uint256 first,uint256 count) external {require(to!=address(0),"ZERO");for(uint256 i;i<count;i++){uint256 id=first+i;require(_owners[id]==address(0),"EXISTS");_owners[id]=to;balanceOf[to]++;emit Transfer(address(0),to,id);}}
    function setLocked(uint256 id,bool value) external {locked[id]=value;}
    function approve(address who,uint256 id) external {address owner=ownerOf(id);require(msg.sender==owner||isApprovedForAll[owner][msg.sender],"AUTH");_approved[id]=who;emit Approval(owner,who,id);}
    function setApprovalForAll(address operator,bool approved) external {require(operator!=msg.sender,"SELF");isApprovedForAll[msg.sender][operator]=approved;emit ApprovalForAll(msg.sender,operator,approved);}
    function transferFrom(address from,address to,uint256 id) public {
        address owner=ownerOf(id);require(owner==from&&to!=address(0),"OWNER");require(!locked[id],"LOCKED");
        require(msg.sender==owner||msg.sender==_approved[id]||isApprovedForAll[owner][msg.sender],"AUTH");
        delete _approved[id];_owners[id]=to;balanceOf[from]--;balanceOf[to]++;emit Transfer(from,to,id);
    }
    function safeTransferFrom(address from,address to,uint256 id) external {
        transferFrom(from,to,id);
        if(to.code.length!=0)require(ITestNftReceiver(to).onERC721Received(msg.sender,from,id,"")==0x150b7a02,"RECEIVER");
    }
}

/// @dev Buyer account that can reject cards, attempt reentrancy or forward the received card away.
contract NftMarketReceiver is ITestNftReceiver {
    bool public reject;
    address public reentryTarget;
    bytes public reentryData;
    bool public attempted;
    bool public reentrySucceeded;
    bytes4 public lastErr;
    address public forwardCardTo;
    function configure(bool rejectCard,address target,bytes calldata data,address forwardTo) external {
        reject=rejectCard;reentryTarget=target;reentryData=data;forwardCardTo=forwardTo;
        attempted=false;reentrySucceeded=false;lastErr=bytes4(0);
    }
    function execute(address target,bytes calldata data) external returns(bytes memory){
        (bool ok,bytes memory result)=target.call(data);
        if(!ok){assembly{revert(add(result,32),mload(result))}}
        return result;
    }
    function onERC721Received(address,address,uint256 id,bytes calldata) external returns(bytes4){
        require(!reject,"CARD_REJECTED");
        if(reentryTarget!=address(0)){
            attempted=true;(bool ok,bytes memory result)=reentryTarget.call(reentryData);reentrySucceeded=ok;
            if(!ok&&result.length>=4)lastErr=bytes4(result);
        }
        if(forwardCardTo!=address(0))NftMarketMock(msg.sender).transferFrom(address(this),forwardCardTo,id);
        return 0x150b7a02;
    }
}
