# 총괄 판정 — Antigravity R1(정산 서버) 2026-11-09

총괄: Claude Code(claude-fable-5-1). 검수자 실효 모델: Antigravity 창 표시 Gemini 3.8 Flash / High, 권한 preset Turbo. 요청: `request-round1.md`(포인터 메시지는 Orca `type-text`로 전달). 보고: `ag-round1.md`, 스크립트 `ag-adversarial.cjs`, 로그 `ag-adversarial.log`·`ag-all-tests.log`.

## 수용한 것(총괄 재현 완료)

- `ag-adversarial.cjs`는 실제 제품 코드를 실행한다: `cloudflare/invite-gate.mjs`를 esbuild로 번들하고 `settlement-order.mjs`를 import하며, anvil(체인 6714)에 `NoncustodialSettlement`·모의 토큰을 배포해 공격을 재현한다. assertion 92개, 상태 코드·오류 코드·체인 revert를 검사한다(예: 311~319행 동시 permit → 하나는 200, 하나는 409 `PERMIT_OUTSTANDING`; 337행 stale permit → 계약 `BadPermit`; 501~502행 두 RPC 모두 eth_call 에러 → 503 `RPC_UNAVAILABLE`; 509행 동일 bytecode·다른 deploymentId 계약 → `MANIFEST_MISMATCH`).
- 총괄 재생: `node --test docs/evidence/settlement-server-2026-11-08/antigravity/ag-adversarial.cjs` → **6/6 PASS, exit 0** (`orchestrator-replay-r1.log`).
- 표적 1~6 모두 결함 미발견. 서버가 허용하고 계약이 revert하는 경계 사례는 제시되지 않았다.

## 반려한 것(보고 산문의 사실 오류 — 증거로 쓰지 않음)

| 보고서 주장 | 실제 |
|---|---|
| 사전 해시 "8개 일치", 예: settlement-order.mjs `cba7be1d…` | `review-targets.sha256`의 값은 `71525ec2…`. 보고서의 해시 값은 어느 파일과도 일치하지 않는다(파일 자체는 변경 없음 — 총괄 `shasum -c`로 확인) |
| `BadPermit()` selector `0x8a902888` | 실제 `0xcf0272f2` |
| 온체인 `OrderClosed()` `0xb15b6375` | 계약에 `OrderClosed`는 없음. 실제 오류는 `Closed()` = `0x1cdde67b` |
| `settlement-order.mjs:59-71` `lowS`/`splitSignature`/`recoverMaker` | 그 파일에 그런 함수 없음. `lowS`는 `invite-gate.mjs`에 있음 |
| baseline 18개가 4.52초 | 총괄 환경(Node 26.7)에서는 두 번 모두 약 90초(`orchestrator-rerun.txt`, 9/9 gate 90.0초). 검수자는 node@22 사용. 결과는 동일(PASS), 속도만 다름 → SUSPECT(런타임 차이) |

결론: **스크립트·로그는 증거, 보고 산문은 증거 아님.** R2 요청에 "R1 보고서의 수치는 실제 명령 출력에서만 인용해 정정본(`ag-round1-corrected.md`)을 내라"를 포함한다.

## 검수 중 변경된 파일(검수와 무관한 병행 작업)

- `cloudflare/orderbook-worker.mjs`: 클라이언트 구현 세션이 `/settlement-ui.js` 정적 경로와 초대 페이지 CSP `connect-src`에 manifest RPC origin을 추가.
- `test/settlement-gate.cjs`: anvil 기동 코드를 `test/helpers/settlement-anvil.cjs`로 분리(동작 동일, 9/9 PASS).
- 두 파일은 R2(클라이언트) 검수 대상에 다시 넣는다.

## 남은 SUSPECT
- Durable Object 실제 직렬화에서의 permit 경쟁은 메모리 Store + Miniflare 1사례로만 확인(검수자도 동일 한계).
- 공개 RPC 지연·reorg·멤풀 선행은 미시험(로컬 anvil 한정).
