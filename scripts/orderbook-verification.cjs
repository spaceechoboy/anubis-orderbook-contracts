const E = require('ethers');

const lot = 10n ** 12n;
const pageSize = 64n;
const rpcBatchSize = 16;
const logWindow = 2048n;
const tokenAbi = new E.Interface(['function balanceOf(address) view returns (uint256)']);

async function deferredRecipients(rpc, deployment, book, readCount, block, finalCount) {
  const topic = book.getEvent('PayoutDeferred').topicHash;
  const address = deployment.address.toLowerCase();
  const recipients = new Map([[deployment.tokenA, new Set()], [deployment.tokenB, new Set()]]);
  const matches = log => log.address.toLowerCase() === address && log.topics[0] === topic;

  async function logsFromReceipts(height) {
    const header = await rpc('eth_getBlockByNumber', [E.toQuantity(height), false]);
    if (!header || !Array.isArray(header.transactions)) throw Error('Payout block unavailable');
    const logs = [];
    for (let i = 0; i < header.transactions.length; i += rpcBatchSize) {
      const hashes = header.transactions.slice(i, i + rpcBatchSize);
      const receipts = await Promise.all(hashes.map(hash => rpc('eth_getTransactionReceipt', [hash])));
      for (const [index, receipt] of receipts.entries()) {
        if (receipt?.blockHash !== header.hash || receipt.transactionHash !== hashes[index]) {
          throw Error('Payout receipt unavailable or moved');
        }
        logs.push(...receipt.logs.filter(matches));
      }
    }
    return logs;
  }

  async function record(logs, first, last, expected) {
    if (BigInt(logs.length) !== expected) return false;
    const ids = new Set();
    const blocks = new Map();
    for (const log of logs) {
      const height = BigInt(log.blockNumber);
      const id = height + ':' + BigInt(log.logIndex);
      if (log.removed || !matches(log) || height < first || height > last || ids.has(id)) return false;
      ids.add(id);
      if (blocks.has(height) && blocks.get(height) !== log.blockHash) return false;
      blocks.set(height, log.blockHash);
    }
    const entries = [...blocks];
    for (let i = 0; i < entries.length; i += rpcBatchSize) {
      const canonical = await Promise.all(entries.slice(i, i + rpcBatchSize).map(async ([height, expectedHash]) => {
        const header = height === BigInt(block.number) ? block : await rpc('eth_getBlockByNumber', [E.toQuantity(height), false]);
        return header?.hash === expectedHash && E.isHexString(expectedHash, 32);
      }));
      if (canonical.includes(false)) return false;
    }
    for (const log of logs) {
      const { args } = book.parseLog(log);
      const accounts = recipients.get(args.token);
      if (!accounts) throw Error('Unknown payout token');
      accounts.add(args.recipient);
    }
    return true;
  }

  async function scan(first, last, before, after) {
    const expected = after - before;
    if (expected < 0n) throw Error('Payout counter decreased during reconciliation');
    if (expected === 0n) return;
    if (last - first < logWindow) {
      const logs = await rpc('eth_getLogs', [{
        address: deployment.address, topics: [topic], fromBlock: E.toQuantity(first), toBlock: E.toQuantity(last),
      }]).catch(() => null);
      if (Array.isArray(logs) && await record(logs, first, last, expected)) return;
    }
    if (first === last) {
      const recovered = await logsFromReceipts(first);
      if (!await record(recovered, first, last, expected)) throw Error('Incomplete payout logs at block ' + first);
      return;
    }
    const middle = (first + last) / 2n;
    const midway = await readCount(middle);
    await scan(first, middle, before, midway);
    await scan(middle + 1n, last, midway, after);
  }

  await scan(BigInt(deployment.deployBlock), BigInt(block.number), 0n, finalCount);
  return recipients;
}

async function verificationSnapshot(rpc, deployment, abi, blockTag) {
  const book = new E.Interface(abi);
  const block = await rpc('eth_getBlockByNumber', [blockTag, false]);
  if (!block || Number(block.number) < deployment.deployBlock) return null;
  const read = async (target, iface, name, args, at) => {
    const result = await rpc('eth_call', [{ to: target, data: iface.encodeFunctionData(name, args) }, at]);
    return iface.decodeFunctionResult(name, result)[0];
  };
  const atSnapshot = { blockHash: block.hash, requireCanonical: true };
  const readBook = (name, args = [], at = atSnapshot) => read(deployment.address, book, name, args, at);
  const [count, payoutCount, active, maximum, pending, after, delay, recipient, authority] = await Promise.all([
    readBook('orderCount'), readBook('deferredPayoutCount'), readBook('activeFeeBps'), readBook('MAX_FEE_BPS'),
    readBook('pendingFeeBps'), readBook('feeChangeAfter'), readBook('FEE_CHANGE_DELAY'), readBook('FEE_RECIPIENT'),
    readBook('FEE_AUTHORITY'),
  ]);

  const reserves = [0n, 0n];
  for (let first = 1n; first <= count; first += pageSize) {
    const expected = count - first + 1n < pageSize ? count - first + 1n : pageSize;
    const orders = await readBook('getOrdersById', [first, pageSize]);
    if (BigInt(orders.length) !== expected || orders.some(order => order.maker === E.ZeroAddress)) {
      throw Error('Incomplete order page starting at ' + first);
    }
    for (const order of orders) {
      if (order.status !== 0n) continue;
      const remaining = order.lots - order.filled;
      if (order.buy) {
        const gross = remaining * order.price * 1_000_000n;
        reserves[1] += gross + gross * order.takerFeeBps / 10_000n;
      } else {
        reserves[0] += remaining * lot;
      }
    }
  }

  async function readCount(height) {
    const header = await rpc('eth_getBlockByNumber', [E.toQuantity(height), false]);
    if (!header) throw Error('Payout counter block unavailable');
    return readBook('deferredPayoutCount', [], { blockHash: header.hash, requireCanonical: true });
  }
  const recipients = await deferredRecipients(rpc, deployment, book, readCount, block, payoutCount);
  const escrow = {};
  const tokens = [['AiLGNS', deployment.tokenA], ['DAI', deployment.tokenB]];
  for (const [i, [symbol, token]] of tokens.entries()) {
    let claims = 0n;
    let batch = [];
    for (const account of recipients.get(token)) {
      batch.push(readBook('claimable', [token, account]));
      if (batch.length === rpcBatchSize) {
        for (const amount of await Promise.all(batch)) claims += amount;
        batch = [];
      }
    }
    for (const amount of await Promise.all(batch)) claims += amount;
    const balance = await read(token, tokenAbi, 'balanceOf', [deployment.address], atSnapshot);
    escrow[symbol] = {
      balanceWei: String(balance), orderReserveWei: String(reserves[i]), claimableWei: String(claims),
      differenceWei: String(balance - reserves[i] - claims),
    };
  }

  const confirmedBlock = await rpc('eth_getBlockByNumber', [block.number, false]);
  if (confirmedBlock?.hash !== block.hash) throw Error('Verification block changed during reconciliation');
  return {
    block: { number: Number(block.number), hash: block.hash, timestamp: Number(block.timestamp) },
    coverage: { orders: String(count), deferredPayouts: String(payoutCount) },
    fee: {
      activeBps: Number(active), maxBps: Number(maximum), delaySeconds: Number(delay), recipient, authority,
      pending: after === 0n ? null : { bps: Number(pending), executableAfter: Number(after) },
    },
    escrow,
  };
}

module.exports = { verificationSnapshot };
