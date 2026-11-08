const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { test } = require('node:test');
const solc = require('solc');
const E = require('ethers');
const { verificationSnapshot } = require('../scripts/orderbook-verification.cjs');

const sourcePath = 'contracts/P2POrderBook.sol';
const compiled = JSON.parse(solc.compile(JSON.stringify({
  language: 'Solidity',
  sources: { [sourcePath]: { content: readFileSync(sourcePath, 'utf8') } },
  settings: { outputSelection: { '*': { '*': ['abi'] } } },
})));
const abi = compiled.contracts[sourcePath].P2POrderBook.abi;
const book = new E.Interface(abi);
const token = new E.Interface(['function balanceOf(address) view returns (uint256)']);
const addr = value => E.getAddress(E.toBeHex(value, 20));
const hash = value => E.toBeHex(value, 32);
const deployment = { address: addr(1), tokenA: addr(2), tokenB: addr(3), deployBlock: 1 };
const account = addr(4);
const selected = 5000n;
const batchHeight = 3000n;
const block = {
  number: E.toQuantity(selected), hash: hash(selected), timestamp: '0x100',
  transactions: [],
};

function rpcFixture({ missingReceipt = false, shortPage = false, staleLogs = false } = {}) {
  const orderCount = 50_003n;
  const payoutCount = 1101n;
  const [topic, tokenTopic, recipientTopic] = book.encodeFilterTopics('PayoutDeferred', [deployment.tokenA, account]);
  const deferred = Array.from({ length: Number(payoutCount) }, (_, i) => ({
    address: deployment.address, topics: [topic, tokenTopic, recipientTopic],
    blockNumber: E.toQuantity(batchHeight), blockHash: hash(batchHeight), logIndex: E.toQuantity(i),
    data: E.AbiCoder.defaultAbiCoder().encode(['uint256'], [1]),
  }));
  const [maker, expiry, buy, open, price, lots, filled, fee] = [account, 0, false, 0, 800_000_000, 2, 1, 100];
  const reserve = orderCount * 10n ** 12n;
  const counters = { pages: 0, logQueries: 0, receipts: 0, claimReads: 0 };

  async function rpc(method, [query, at]) {
    if (method === 'eth_getBlockByNumber') {
      if (BigInt(query) === selected) return block;
      return { hash: hash(BigInt(query)), transactions: [hash(99)] };
    }
    if (method === 'eth_getLogs') {
      ++counters.logQueries;
      const first = BigInt(query.fromBlock);
      const last = BigInt(query.toBlock);
      if (first <= batchHeight && batchHeight <= last) {
        return staleLogs ? deferred.map(log => ({ ...log, blockHash: hash(999) })) : deferred.slice(0, 500);
      }
      return [];
    }
    if (method === 'eth_getTransactionReceipt') {
      ++counters.receipts;
      return { transactionHash: query, blockHash: hash(batchHeight), logs: missingReceipt ? deferred.slice(1) : deferred };
    }
    assert.equal(method, 'eth_call');
    assert.equal(at.requireCanonical, true);
    if (query.to !== deployment.address) {
      return token.encodeFunctionResult('balanceOf', [query.to === deployment.tokenA ? reserve + payoutCount : 0n]);
    }
    const { name, args } = book.parseTransaction(query);
    let result;
    switch (name) {
      case 'getOrdersById': {
        ++counters.pages;
        const remaining = orderCount - args[0] + 1n;
        const size = remaining < args[1] ? remaining : args[1];
        result = Array.from({ length: Number(size) - Number(shortPage) }, () => [maker, expiry, buy, open, price, lots, filled, fee]);
        break;
      }
      case 'orderCount': result = orderCount; break;
      case 'deferredPayoutCount': result = BigInt(at.blockHash) < batchHeight ? 0n : payoutCount; break;
      case 'claimable': ++counters.claimReads; result = payoutCount; break;
      case 'FEE_AUTHORITY':
      case 'FEE_RECIPIENT': result = E.ZeroAddress; break;
      default: result = 0;
    }
    return book.encodeFunctionResult(name, [result]);
  }
  return { rpc, counters, reserve, payoutCount };
}

test('reconciles 50,003 orders and recovers 1,101 truncated payout logs from receipts', async () => {
  const { rpc, counters, reserve, payoutCount } = rpcFixture();
  const result = await verificationSnapshot(rpc, deployment, abi, block.number);
  assert.deepEqual(result.coverage, { orders: '50003', deferredPayouts: '1101' });
  assert.deepEqual(result.escrow.AiLGNS, {
    balanceWei: String(reserve + payoutCount), orderReserveWei: String(reserve),
    claimableWei: String(payoutCount), differenceWei: '0',
  });
  assert.equal(counters.pages, 782);
  assert.equal(counters.receipts, 1);
  assert.equal(counters.claimReads, 1);
  assert(counters.logQueries > 1);
});

test('rejects incomplete order pages and receipt logs', async () => {
  for (const [options, error] of [
    [{ shortPage: true }, /Incomplete order page/],
    [{ missingReceipt: true }, /Incomplete payout logs/],
  ]) {
    await assert.rejects(verificationSnapshot(rpcFixture(options).rpc, deployment, abi, block.number), error);
  }
});

test('recovers complete-looking logs from a stale fork using canonical receipts', async () => {
  const { rpc, counters } = rpcFixture({ staleLogs: true });
  const result = await verificationSnapshot(rpc, deployment, abi, block.number);
  assert.equal(result.escrow.AiLGNS.differenceWei, '0');
  assert.equal(counters.receipts, 1);
});
