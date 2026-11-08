// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
// SIMULATION ONLY: install runtime using eth_call state overrides, never deploy.
interface ProbeToken { function balanceOf(address) external view returns(uint256); function transfer(address,uint256) external returns(bool); }
interface ProbeNFT { function ownerOf(uint256) external view returns(address); function transferFrom(address,address,uint256) external; }
contract AssetTransferProbe {
 function roundTrip(address token,address peer,uint256 amount) external returns(uint256 debit,uint256 credit,uint256 backDebit,uint256 backCredit) {
  uint256 a=ProbeToken(token).balanceOf(address(this)); uint256 b=ProbeToken(token).balanceOf(peer);
  require(ProbeToken(token).transfer(peer,amount),"outbound false");
  uint256 c=ProbeToken(token).balanceOf(address(this)); uint256 d=ProbeToken(token).balanceOf(peer);
  debit=a-c;credit=d-b;
  AssetTransferProbe(peer).sendBack(token,address(this),credit);
  backDebit=d-ProbeToken(token).balanceOf(peer);backCredit=ProbeToken(token).balanceOf(address(this))-c;
 }
 function sendBack(address token,address to,uint256 amount) external { require(ProbeToken(token).transfer(to,amount),"return false"); }
 function nftRoundTrip(address nft,address peer,uint256 id) external returns(address mid,address end) {
  require(ProbeNFT(nft).ownerOf(id)==address(this),"not owner");
  ProbeNFT(nft).transferFrom(address(this),peer,id);mid=ProbeNFT(nft).ownerOf(id);
  AssetTransferProbe(peer).nftBack(nft,address(this),id);end=ProbeNFT(nft).ownerOf(id);
 }
 function nftBack(address nft,address to,uint256 id) external {ProbeNFT(nft).transferFrom(address(this),to,id);}
}
