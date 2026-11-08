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

배포 거래: `0x02abedcf8901c13a1ca6dc14d1be0f77d3ce871588276f3544ba5388ec545b1a`.

AiLGNS 배포는 테이커 수수료 100bps(1%), 상한100bps, 변경권한0주소, 지연0으로 생성됐습니다. 일반화된 소스에는 변경 함수가 있으나 이 인스턴스에서는 변경권한이 없어 호출할 수 없습니다. 메이커0%, 테이커는 DAI로 정산합니다. 계약은 프록시가 아닙니다. 자산 토큰 자체의 권한·전송 제한·업그레이드 위험은 별도입니다.

- [배포 파라미터](deployments/anubis-orderbook-fixed1pct.json)
- [기준 블록 대사 스냅샷](deployments/anubis-orderbook-fixed1pct-verification.json): block18525449, 당시 주문0건. 현재 예치금 상태를 뜻하지 않습니다.
- [HTTPS 검증 화면](https://anubis-guide-orderbook-staging.awakeorigindirect.workers.dev/verify)
- [기계 판독 검증 자료](https://anubis-guide-orderbook-staging.awakeorigindirect.workers.dev/verification.json)

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

컴파일러 `solc 0.8.24+commit.e11b9ed9`, optimizer200, EVM paris, metadata bytecodeHash ipfs. build.py가 `build/artifacts.json`과 핵심 계약 4개의 `build/<Contract>.standard-input.json` (AssetTransferProbe 제외)을 생성합니다. Solidity 원본 경로도 메타데이터에 포함되므로 유지하세요.

배포 코드·생성자·기준 블록 예치금 재검증:

```sh
npm run verify:deployed -- https://rpc4.anubispace.org
```

위 검사는 생성 트랜잭션 전체 입력, runtime hash, immutable 영역을 제외한 컴파일 runtime, immutable을 포함하는 생성 인자와 getter, 기준 블록 잔액/지급 대기 장부를 대조합니다. RPC가 해당 과거 블록을 지원하지 않으면 실패하며 성공으로 취급하지 않습니다. 검사 경로는 읽기 전용 JSON-RPC입니다. 테스트는 로컬 모의 EVM에서만 거래합니다.

## 탐색기 검증과 한계

GitHub 공개는 탐색기 소스 검증이나 외부 감사를 대신하지 않습니다. 탐색기 검증 완료 여부는 별도 확인 대상입니다. 독립 컴파일 입력은 위 명령으로 재생성하고 실제 배포 코드와 비교하세요. 과거 블록의 회계 일치는 현재/미래 지급 가능성이나 토큰 발행자의 정책을 보장하지 않습니다. 신규 계약 테스트 통과는 배포·활성화 완료가 아닙니다.

## 출처와 공개 범위

2026-11-08 기존 프로젝트에서 명시적으로 선별한 공개 스냅샷입니다. 과거 비공개 Git 이력은 포함하지 않습니다. 계약5개는 원본 바이트 그대로입니다. 검증 JS의 ethers 로딩만 고정 npm 패키지로 바꾸고, 공개 CLI에서 내부 웹사이트 정적파일 묶기 옵션은 제거했습니다. ethers 및 solc의 라이선스는 각 npm 패키지를 따릅니다.

취약점 재현에는 로컬 환경을 사용하세요. 실제 사용자 자산을 대상으로 공격하거나 테스트하지 마세요.
