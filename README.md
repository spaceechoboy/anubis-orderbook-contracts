# Anubis Guide — Order Book Contract Verification

공개 계약 소스, 로컬 회계 테스트, 재현 가능한 컴파일 설정 및 배포 근거입니다. **외부 보안 감사 완료 또는 자금 안전 보장을 의미하지 않습니다.** 프론트엔드·운영 서버·비밀값은 이 저장소의 공개 범위가 아닙니다.

## 계약과 배포 상태 (2026-11-08)

| 소스 | 용도 | 상태 |
|---|---|---|
| [P2POrderBook.sol](contracts/P2POrderBook.sol) | AiLGNS/DAI 가격·시간 우선 주문장 | Anubis 6714, `0x2D69c328a260f4BdabE85Ff588436DF74b9FE042` |
| [P2PTokenOrderBook.sol](contracts/P2PTokenOrderBook.sol) | LGNS(9 decimals), gLGNS(18 decimals), 시장별 별도 인스턴스 | 미배포 |
| [AwakeNftMarket.sol](contracts/AwakeNftMarket.sol) | NFT 판매 등록·DAI 매수 입찰 | 미배포 |
| [P2PEscrow.sol](contracts/P2PEscrow.sol) | 이전 개별 주문 수락 설계 | 현재 AiLGNS 주문장 아님; 회귀 테스트용 포함 |
| [AssetTransferProbe.sol](contracts/AssetTransferProbe.sol) | 읽기 전용 시뮬레이션 보조 | eth_call code override 전용. 배포하거나 자산을 보내지 마세요. |
| [NoncustodialSettlement.sol](contracts/NoncustodialSettlement.sol) | 비수탁 초대 정산(EIP-712 maker 주문 + 게이트 permit, 지갑 간 직접 교환) | **미배포.** 2026-11-09 추가 공개. 아래 절 참조. |

배포 거래: `0x02abedcf8901c13a1ca6dc14d1be0f77d3ce871588276f3544ba5388ec545b1a`.

AiLGNS 배포는 테이커 수수료 100bps(1%), 상한100bps, 변경권한0주소, 지연0으로 생성됐습니다. 일반화된 소스에는 변경 함수가 있으나 이 인스턴스에서는 변경권한이 없어 호출할 수 없습니다. 메이커0%, 테이커는 DAI로 정산합니다. 계약은 프록시가 아닙니다. 자산 토큰 자체의 권한·전송 제한·업그레이드 위험은 별도입니다.

- [배포 파라미터](deployments/anubis-orderbook-fixed1pct.json)
- [기준 블록 대사 스냅샷](deployments/anubis-orderbook-fixed1pct-verification.json): block18525449, 당시 주문0건. 현재 예치금 상태를 뜻하지 않습니다.
- [HTTPS 검증 화면](https://anubis-guide-orderbook-staging.awakeorigindirect.workers.dev/verify)
- [기계 판독 검증 자료](https://anubis-guide-orderbook-staging.awakeorigindirect.workers.dev/verification.json)

## 비수탁 초대 정산 계약 (2026-11-09 추가 공개)

`NoncustodialSettlement.sol`은 자산이 주문 대기 동안 사용자 지갑에 남고, 한 트랜잭션 안에서 지갑 간 토큰/NFT와 DAI를 직접 교환한 뒤 양쪽 잔액을 대사하는 정산 계약입니다. **어떤 체인에도 배포하지 않았고, 운영 게이트 서명 키는 구성하지 않았으며, 출시 승인이 아닙니다.** 기존 공개 주문장 5개 계약과 그 배포 상태를 대체하지 않습니다. 운영 서버·초대 API·프런트엔드·키 관리 런북은 공개 범위가 아닙니다.

- 소스 SHA256: `497cf914cc602c641790267c1dd3984be921f8e8d3fe9dfca13cd67bc7625c6c` ([SOURCE-HASHES.json](SOURCE-HASHES.json)). solc 0.8.24 / optimizer 200 / EVM paris / viaIR 미사용. creation 14,260 bytes, runtime 12,309 bytes.
- [독립 컴파일 입력](artifacts/noncustodial-settlement/NoncustodialSettlement.standard-input.json)과 [ABI](artifacts/noncustodial-settlement/NoncustodialSettlement.abi.json): `npm run build`가 `build/`에 생성하는 것과 byte-for-byte 동일한 사본입니다(`build/`는 git 제외).
- 시험: [계약 시험](test/test_noncustodial_settlement.py) 32건, [독립 공격 시험](test/test_settlement_adversarial.py) 12건(모의 토큰 [SettlementMocks.sol](test/mocks/SettlementMocks.sol)). 기존 `python -m unittest discover` 명령에 포함됩니다.
- 변이 시험: `.venv/bin/python scripts/test-settlement-mutations.py` — 수수료 절반·잔액 대사 제거·nonce 취소 무시 세 가지를 메모리 안에서만 컴파일해 기존 assertion이 잡는지 확인합니다(`build/noncustodial-mutations.json` 생성). 전수 mutation coverage가 아닙니다.
- 명세: [최대 24시간 초대 거래와 비수탁 정산](docs/specs/2026-11-08-noncustodial-invite-settlement.md). 서버·UI 관련 문단은 비공개 구현을 설명하며 그 코드는 이 저장소에 없습니다.
- 검수 증거: [docs/evidence/noncustodial-settlement-2026-11-08/](docs/evidence/noncustodial-settlement-2026-11-08/) — 시험 로그, 변이 결과, 11개 검수 대상 해시 목록(`audit-source-round2.json`; `cloudflare/`·`terminal/`·`scripts/check-noncustodial-creation.cjs`·`test/invite-gate.cjs` 5개 항목은 비공개 파일의 해시만 기록). [정산 서버 검수 총괄 판정](docs/evidence/settlement-server-2026-11-08/orchestrator-verdict-r1.md)은 비공개 서버 코드에 대한 판정 기록입니다.

검증한 것은 로컬 모의 EVM에서 정상 경로의 예치/채무 증가가 없고 시험한 실패가 원자적으로 롤백된다는 점입니다. 실제 토큰의 미래 동작, 특정 지갑 앱 호환성, 운영 키 안전, 탐색기 검증 완료를 뜻하지 않습니다. 게이트 서명자는 생성자에서 고정되며 permit은 매 체결 300초 이내여야 합니다.

## 재현

Node.js22, Python3.12 권장. 키·시드·지갑 연결이 필요하지 않습니다.

```sh
npm ci --ignore-scripts
npm run build
npm test
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s test -p 'test_*.py'
```

컴파일러 `solc 0.8.24+commit.e11b9ed9`, optimizer200, EVM paris, metadata bytecodeHash ipfs. build.py가 `build/artifacts.json`과 핵심 계약 5개의 `build/<Contract>.standard-input.json` (AssetTransferProbe 제외)을 생성합니다. Solidity 원본 경로도 메타데이터에 포함되므로 유지하세요.

배포 코드·생성자·기준 블록 예치금 재검증:

```sh
npm run verify:deployed -- https://rpc4.anubispace.org
```

위 검사는 생성 트랜잭션 전체 입력, runtime hash, immutable 영역을 제외한 컴파일 runtime, immutable을 포함하는 생성 인자와 getter, 기준 블록 잔액/지급 대기 장부를 대조합니다. RPC가 해당 과거 블록을 지원하지 않으면 실패하며 성공으로 취급하지 않습니다. 검사 경로는 읽기 전용 JSON-RPC입니다. 테스트는 로컬 모의 EVM에서만 거래합니다.

## 탐색기 검증과 한계

GitHub 공개는 탐색기 소스 검증이나 외부 감사를 대신하지 않습니다. 탐색기 검증 완료 여부는 별도 확인 대상입니다. 독립 컴파일 입력은 위 명령으로 재생성하고 실제 배포 코드와 비교하세요. 과거 블록의 회계 일치는 현재/미래 지급 가능성이나 토큰 발행자의 정책을 보장하지 않습니다. 신규 계약 테스트 통과는 배포·활성화 완료가 아닙니다.

## 출처와 공개 범위

2026-11-08 기존 프로젝트에서 명시적으로 선별한 공개 스냅샷입니다. 과거 비공개 Git 이력은 포함하지 않습니다. 계약5개는 원본 바이트 그대로입니다. 2026-11-09에 NoncustodialSettlement.sol·모의 토큰·시험·변이 스크립트·명세·검수 증거를 같은 방식(원본 byte-for-byte, 경로만 일부 이동)으로 추가했습니다. 개인 절대 경로가 들어 있는 검수 원문 2건은 공개하지 않았습니다. 검증 JS의 ethers 로딩만 고정 npm 패키지로 바꾸고, 공개 CLI에서 내부 웹사이트 정적파일 묶기 옵션은 제거했습니다. ethers 및 solc의 라이선스는 각 npm 패키지를 따릅니다.

취약점 재현에는 로컬 환경을 사용하세요. 실제 사용자 자산을 대상으로 공격하거나 테스트하지 마세요.
