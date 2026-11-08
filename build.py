import json,subprocess,os
SOLC=[os.environ["SOLC"],"--standard-json"] if os.environ.get("SOLC") else ["node","scripts/solc.cjs"]
def read(p): return open(p).read()
inp={"language":"Solidity","sources":{path:{"content":read(path)} for path in (
         "contracts/P2PEscrow.sol", "contracts/P2POrderBook.sol", "contracts/P2PTokenOrderBook.sol", "contracts/AwakeNftMarket.sol", "test/mocks/Mocks.sol", "test/mocks/TokenBookMocks.sol", "test/mocks/NftMarketMocks.sol")},
     "settings":{"optimizer":{"enabled":True,"runs":200},"evmVersion":"paris","metadata":{"bytecodeHash":"ipfs"},
                 "outputSelection":{"*":{"*":["abi","evm.bytecode.object","evm.deployedBytecode.object","evm.deployedBytecode.immutableReferences","metadata"]}}}}
compiled=subprocess.run(SOLC,input=json.dumps(inp),capture_output=True,text=True)
if compiled.returncode: raise SystemExit(compiled.stderr or "compiler failed")
out=json.loads(compiled.stdout)
errs=[e for e in out.get("errors",[]) if e["severity"]=="error"]
if errs: print(json.dumps(errs,indent=1)); raise SystemExit(1)
for e in out.get("errors",[]): print("warning:",e["formattedMessage"][:200])
os.makedirs("build",exist_ok=True)
arts={}
for f,cs in out["contracts"].items():
    for name,c in cs.items():
        arts[name]={"abi":c["abi"],"bin":c["evm"]["bytecode"]["object"],"deployed":c["evm"]["deployedBytecode"]["object"],"immutableReferences":c["evm"]["deployedBytecode"].get("immutableReferences",{}),"metadata":c.get("metadata")}
json.dump(arts,open("build/artifacts.json","w"))
# 각 계약의 독립 컴파일·검증용 입력 (메타데이터 포함)
for name in ("P2PEscrow", "P2POrderBook", "P2PTokenOrderBook", "AwakeNftMarket"):
    path = f"contracts/{name}.sol"
    standard = {**inp, "sources": {path: inp["sources"][path]}}
    with open(f"build/{name}.standard-input.json", "w") as f:
        json.dump(standard, f, indent=1)
print("built:",", ".join(f"{k}({len(v['bin'])//2}B)" for k,v in arts.items()))
