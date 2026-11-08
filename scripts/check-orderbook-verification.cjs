const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const E = require('ethers');
const { verificationSnapshot } = require('./orderbook-verification.cjs');

const root = path.resolve(__dirname, '..');

async function main(evidencePath, rpcUrl, flag, stagePath, ...extra) {
  assert(evidencePath && rpcUrl && !flag && !stagePath && !extra.length,
    'Usage: node scripts/check-orderbook-verification.cjs <evidence.json|-> <RPC URL>');
  const endpoint = new URL(rpcUrl);
  assert(['https:', 'http:'].includes(endpoint.protocol), 'RPC must use HTTP or HTTPS');
  const evidence = JSON.parse(readFileSync(evidencePath === '-' ? 0 : evidencePath, 'utf8'));
  const { deployment, block } = evidence;
  assert(Number.isSafeInteger(block.number) && Number.isSafeInteger(deployment.deployBlock), 'Invalid block number');
  assert(block.number >= deployment.deployBlock && deployment.deployBlock > 0, 'Block predates deployment');
  assert(E.isHexString(deployment.deployTx, 32) && E.isHexString(block.hash, 32), 'Invalid transaction or block hash');
  const headers = { 'Content-Type': 'application/json' };
  if (endpoint.hostname === '127.0.0.1') headers.Origin = endpoint.origin;

  async function rpc(method, params = []) {
    const response = await fetch(endpoint, {
      method: 'POST', headers, signal: AbortSignal.timeout(15_000),
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }),
    });
    assert(response.ok, method + ' HTTP ' + response.status);
    const payload = await response.json();
    assert(payload.id === 1 && !payload.error && 'result' in payload, method + ' RPC response unavailable');
    return payload.result;
  }

  const sourcePath = 'contracts/P2POrderBook.sol';
  const source = readFileSync(path.join(root, sourcePath));
  const { P2POrderBook: artifact } = JSON.parse(readFileSync(path.join(root, 'build/artifacts.json'), 'utf8'));
  assert.equal(E.keccak256(source), JSON.parse(artifact.metadata).sources[sourcePath].keccak256, 'Build artifacts are stale');
  assert.equal(createHash('sha256').update(source).digest('hex'), deployment.sourceSha256, 'Source SHA-256 differs');

  const [chain, receipt, transaction, runtime, deployBlock] = await Promise.all([
    rpc('eth_chainId'),
    rpc('eth_getTransactionReceipt', [deployment.deployTx]),
    rpc('eth_getTransactionByHash', [deployment.deployTx]),
    rpc('eth_getCode', [deployment.address, { blockHash: block.hash, requireCanonical: true }]),
    rpc('eth_getBlockByNumber', [E.toQuantity(deployment.deployBlock), false]),
  ]);
  assert.equal(Number(chain), deployment.chainId, 'Chain ID differs');
  assert.equal(Number(receipt?.status), 1, 'Deployment did not succeed');
  assert.equal(receipt.transactionHash, deployment.deployTx, 'Deployment receipt hash differs');
  assert.equal(E.getAddress(receipt.contractAddress), deployment.address, 'Deployment address differs');
  assert.equal(Number(receipt.blockNumber), deployment.deployBlock, 'Deployment block differs');
  assert.equal(transaction?.to, null, 'Expected a direct contract deployment');
  assert.equal(transaction.hash, deployment.deployTx, 'Deployment transaction hash differs');
  assert.equal(transaction.blockHash, receipt.blockHash, 'Deployment transaction and receipt disagree');
  assert(deployBlock?.hash === receipt.blockHash && deployBlock.transactions.includes(deployment.deployTx),
    'Deployment is not in the canonical block');

  const factory = new E.ContractFactory(artifact.abi, '0x' + artifact.bin);
  const creation = await factory.getDeployTransaction(
    deployment.tokenA, deployment.tokenB, deployment.feeRecipient, deployment.feeAuthority,
    deployment.feeBps, deployment.maxFeeBps, deployment.feeChangeDelay,
  );
  assert.equal(transaction.input, creation.data, 'Creation input differs from local build');
  assert.equal(E.keccak256(runtime), deployment.runtimeKeccak, 'Runtime code hash differs');
  const masked = E.getBytes(runtime);
  for (const locations of Object.values(artifact.immutableReferences)) {
    for (const { start, length } of locations) masked.fill(0, start, start + length);
  }
  assert.equal(E.hexlify(masked), '0x' + artifact.deployed, 'Runtime differs from local build');

  const actual = await verificationSnapshot(rpc, deployment, artifact.abi, E.toQuantity(block.number));
  assert(actual, 'Snapshot block unavailable');
  assert.deepEqual(actual.block, block, 'Snapshot block differs');
  assert.deepEqual(actual.coverage, evidence.coverage, 'Snapshot coverage differs');
  assert.deepEqual(actual.fee, evidence.fee, 'Fee configuration differs');
  for (const [symbol, balances] of Object.entries(actual.escrow)) {
    for (const [field, value] of Object.entries(balances)) {
      assert.equal(value, evidence.escrow[symbol][field], 'escrow.' + symbol + '.' + field);
    }
    assert(BigInt(balances.differenceWei) >= 0n, symbol + ' balance is below recorded liabilities');
  }
  console.log('PASS chain ' + deployment.chainId + ' block ' + block.number + ' ' + block.hash);
  console.log(JSON.stringify({ coverage: actual.coverage, fee: actual.fee, escrow: actual.escrow }, null, 2));
}

main(...process.argv.slice(2)).catch(error => {
  console.error(error.code === 'ENOENT' ? 'Input or build artifact missing. Run npm run build first.' : error.message);
  process.exitCode = 1;
});
