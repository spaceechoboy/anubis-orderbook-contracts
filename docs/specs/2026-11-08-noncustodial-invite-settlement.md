# 최대 24시간 초대 거래와 비수탁 정산 명세

기준일: 2026-11-08 KST. 상태: **신규 계약 로컬 구현, 보완·재검증 단계**. 구현은 `contracts/NoncustodialSettlement.sol`이며 현재 예치형 계약을 변경하지 않는다. 초대 UI/API의 `personal_sign`은 접근 확인용이며 주문 서명·토큰 승인·자산 정산 권한이 아니다. 배포와 실제 정산 연동 검증 전에는 `settlementReady:false`를 유지한다. 검증 결과는 [구현 증거](../../evidence/noncustodial-settlement-2026-11-08/README.md)에 별도로 기록한다.

관련: [3환경·초대 거래 목표](2026-11-08-cross-platform-private-trade-goals.md), [멀티 자산 설계](2026-11-08-multi-asset-orderbook-design.md). 최신 사용자 결정: B 주소 직접 입력과 A의 재승인을 요구하지 않음, 4자리 PIN, 모바일 일반 브라우저의 WC 연결·서명·복귀, 최대 24시간 주문, 발행자 동결과 플랫폼 결함을 구분함.

## 1. 일반 지갑으로 가능한 원리

스마트지갑으로 전환할 필요가 없다. 일반 EOA 지갑으로 maker가 조건을 서명하고 필요한 토큰 사용 한도를 승인한다. 주문이 대기하는 동안 자산은 자신의 지갑에 있다. taker가 수락 트랜잭션을 제출하면 정산 계약이 서명을 확인한 뒤 양측 지갑 사이에서 직접 자산을 교환한다. 한 전송이라도 실패하면 같은 트랜잭션의 전송·체결 기록을 전부 되돌린다.

ERC-20의 `approve`/`transferFrom`이 이 이동 권한을 제공한다. 승인은 예치가 아니지만 spender 계약의 사용 권한이므로, 비수탁이어도 승인 악용 위험은 남는다. 신규 spender에 주문에 필요한 한도만 승인하고, 한도를 바꿀 때 필요한 토큰의 `0 → 새 한도` 절차도 처리한다. [ERC-20 표준](https://eips.ethereum.org/EIPS/eip-20)

NFT는 해당 카드의 개별 `approve`를 우선한다. 전체 컬렉션 승인(`setApprovalForAll`)을 기본 요구하지 않는다. 수신자가 계약이면 `safeTransferFrom`의 수신 검사와 전송 후 `ownerOf`를 확인한다. [ERC-721 표준](https://eips.ethereum.org/EIPS/eip-721)

예: A가 100 LGNS를 200 DAI에 판매한다. A는 LGNS를 맡기지 않고 조건을 서명한다. B가 수락하면 A→B로 100 LGNS, B→A로 200 DAI, B→고정 수수료 주소로 2 DAI가 한 트랜잭션에서 이동한다. B는 거래대금과 수수료 202 DAI 및 네이티브 가스를 준비한다. A의 잔액·승인 부족, LGNS 전송 제한, 수수료 지급 실패 중 하나라도 발생하면 전부 실패한다.

## 2. 현재 코드에서 확인한 경계

| 현재 코드 | 실제 동작 | 새 초대 계약과의 차이 |
|---|---|---|
| `contracts/P2POrderBook.sol:170-178` | `placeLimit`은 10분~30일, 자산 예치 후 FIFO 매칭 | PIN 또는 링크로 특정 상대를 제한할 함수가 없음 |
| `contracts/P2PTokenOrderBook.sol:128-136` | LGNS/gLGNS용 9/18 decimals, 예치형 FIFO | 별도 서명 주문 정산 계약 필요 |
| `contracts/P2PTokenOrderBook.sol:332-351` | 메이커 0%, 테이커가 DAI로 1% 부담 | 새 직접 거래에서도 동일 부담 기준 유지 |
| `contracts/AwakeNftMarket.sol:69-78` | 매도 등록 시 NFT는 maker 지갑에 남음 | 이미 카드 보관은 비수탁이지만 초대 제한은 없음 |
| `contracts/AwakeNftMarket.sol:96-119` | 매수 입찰 DAI는 예치, 잔량 취소·만기 반환 | 새 초대 매수는 DAI도 지갑에 둠 |
| 기존 `_pay`/`claimable` | 전송 실패 시 지급 대기 채무가 남을 수 있음 | 새 계약에는 지급 대기 잔액을 만들지 않음; 실패 시 전체 revert |

`terminal/index.html`과 `terminal/nft.js`의 7일 선택지는 이번 구현에서 제거했고, `terminal/app.js`와 `terminal/nft.js`의 주문 생성 입력도 24시간 이하로 검사한다. 초대 API도 최대 24시간을 서버에서 검사한다. **UI 제한이 기존 배포 계약의 30일 상한을 변경한 것은 아니다.** 기존 주문의 만기도 변경하지 않는다. (2026-11-08 최종 문서 대사: 초기 검토 시점의 ‘7일 선택지가 있다’는 설명을 구현 결과에 맞게 정정.)

## 3. 채널과 자산 경계

- 공개 호가/FIFO와 초대 직접 거래는 별도 채널·별도 계약·별도 주문 식별자를 사용한다. 공개 호가 행을 초대 거래로 포장하지 않는다.
- 체인 6714, DAI 주소와 각 base 토큰 주소는 `scripts/multi-asset-markets.cjs`의 레지스트리와 배포 manifest를 대조한다. 임의 토큰 주소를 입력받아 실행하지 않는다.
- 신규 정산 계약은 마켓별 고정 토큰/decimals/수수료 수취인/공식 `GATE_SIGNER`, 비업그레이드 구조다. 요율은 `FEE_BPS=100`, 변경 함수·임의 호출·delegatecall·관리자 사용자자금 인출 함수가 없다. 개선과 게이트 키 교체는 새 배포와 새 사용자 승인으로 한다.
- 초대는 목록 비노출이다. 체결 이후 주소·금액·calldata·이벤트는 공개 체인에서 관찰될 수 있다. 링크+PIN 소지자가 특정 인물 B라는 신원 보장은 하지 않는다.
- 첫 PIN+지갑 소유 확인 성공 지갑을 해당 초대의 recipient로 결박한다. 주소 입력/A 재승인은 없다. 링크+PIN이 함께 유출되면 제3자가 먼저 결박할 수 있으므로 새 링크/새 nonce로 재발급해야 한다.

## 4. 오프체인 접근 확인과 온체인 권한을 분리

### 이번 구현 단계

1. A가 조건·만기·4자리 PIN을 입력하고, 연결 지갑으로 초대 생성 내용을 `personal_sign`한다. 이 서명은 토큰 이동을 허용하지 않는다.
2. 서버는 암호학적 256비트 링크 secret을 한 번 발급한다. 원본 PIN·secret은 서버 저장하지 않고 서버 비밀 pepper를 이용한 검증값을 저장한다. PIN은 URL/로그/분석/온체인에 넣지 않는다.
3. B는 링크에서 PIN을 입력한다. **초대 전체 수명 동안 잘못된 PIN 총 5회**면 잠근다. IP·지갑 변경과 시간 경과로 횟수를 초기화하지 않는다. 예전 목표 문서의 ‘5회 실패 후 15분 잠금’ 후보는 이번 명세에서는 채택하지 않는다. 잠긴 초대는 원 생성자가 새 초대로 대체한다.
4. PIN 성공 후 짧은 접근 토큰을 발급한다. B가 WC 또는 injected로 연결한 지갑의 소유 확인 메시지에 서명한다. 메시지는 origin·chainId·초대 ID·조건 digest·지갑·단회 nonce·5분 이내 만기를 포함한다.
5. 최초 서명 성공 때 recipient를 원자적으로 저장하고, 다른 지갑의 동시/후속 verify를 거부한다. 거래 화면은 조건·만기·연결 지갑을 복원한다. 표시 상태는 **접근 확인 완료 / 정산 계약 준비 중**이며 ‘체결 완료’가 아니다.
6. 접근 티켓은 최대 15분/초대 만기 중 짧은 기간에만 유효하다. 페이지 새로고침 시 서버 상태를 재확인한다. 지갑 앱에서 돌아온 사실만으로 서명 성공을 만들거나 다시 제출하지 않는다.

PIN은 10,000개뿐이다. 난수 링크, 서버 비밀, 전역 실패 횟수, 단회 challenge를 함께 사용한다. 링크 보유자가 고의로 5회 실패하여 초대를 잠그는 서비스 거부 위험은 남는다. 이를 핑계로 실패 제한을 풀지 않는다. 새 초대 발급 경로와 만료 안내를 제공한다.

### 신규 계약 활성화 이후 추가되는 권한

접근용 `personal_sign`을 주문 권한으로 재사용하지 않는다. A는 별도로 아래 EIP-712 `Order`를 서명한다. 게이트 서버는 PIN/지갑 확인 후에만 별도 EIP-712 `GatePermit`을 발급한다. **현재 접근 티켓은 GatePermit이 아니며 정산 계약에 제출할 수 없다.**

EIP-712의 domain에는 `name`, `version`, `chainId`, `verifyingContract`를 포함한다. 표준 자체는 재사용 방지를 제공하지 않으므로 아래 nonce·누적 체결·취소 검사가 필수다. [EIP-712 표준](https://eips.ethereum.org/EIPS/eip-712)

## 5. 서명 데이터와 상태

`Order`의 모든 자산/금액/만기 필드는 정수형이며 정렬·직렬화 규칙을 고정한다.

| 필드 | 결박하는 대상 |
|---|---|
| `maker`, `makerNonce`, `makerEpoch` | 서명자, 주문별 고유 nonce, 전체 주문 취소 세대 |
| `marketId`, `assetKind`, `baseToken`, `quoteToken`, `baseDecimals` | 정확한 체인 자산과 단위; UI symbol만 신뢰하지 않음 |
| `buy`, `priceTicks`, `totalLots`, `minFillLots`, `allowPartial` | maker 방향, DAI 단가, 최대 수량, 부분 체결 조건 |
| `nftMode`, `tokenId` | ERC20이면 0; NFT는 특정 카드 또는 명시 동의한 컬렉션 매수 |
| `validAfter`, `expiresAt` | 최대 86,400초의 실행 가능 구간 |
| `feeBps`, `feeRecipient` | 100 bps와 immutable 수취인과 일치해야 함 |
| `gateSigner`, `inviteDigest`, `singleCounterparty` | immutable 공식 GATE_SIGNER와 일치하는 게이트, 초대 digest, 단일 상대=true |
| `deploymentId` | manifest에 공개한 계약 버전/토큰 설정 digest |

정산 계약 주소는 domain에서 결박된다. `deploymentId`는 배포 설정의 digest이며 실제 runtime hash는 manifest와 RPC에서 별도로 대조한다. 자기 runtime codehash를 생성자에서 계산하여 불변값처럼 오인하지 않는다. 비업그레이드 계약이므로 버전 교체는 주소·domain도 바꾼다.

`GatePermit`은 `orderHash`, `taker`, `fillLots`, `expectedFilledLots`, `permitNonce`, `issuedAt`, `expiresAt`, `chainId`, `verifyingContract`를 고정한다. 만기는 발급 후 최대 300초 및 주문 만기 중 빠른 쪽이다. 초기 120초 후보를 모바일 지갑 왕복 여유를 위해 300초로 보완했다(실기기 지연 측정 완료를 뜻하지 않음). NFT 특정 카드/컬렉션 매수에 제출할 카드 ID도 permit에 포함한다. 서버가 다른 수량/카드를 발급해도 maker 주문의 허용 범위를 넘으면 계약이 거부한다.

계약 상태는 `filledLots[orderHash]`, `cancelled[orderHash]`, `cancelledNonce[maker][nonce]`, `nonceToOrderHash[maker][nonce]`, `epoch[maker]`, `boundTaker[orderHash]`, `usedPermit[permitDigest]`만 보관한다. nonce를 여러 조건에 재사용하면 첫 유효 주문에 결박하고 다른 digest는 거부한다. 첫 성공 체결에서 `boundTaker`를 저장하여 서버가 이후 다른 지갑으로 바꿔 발급해도 거부한다. 첫 체결 전 서버 침해는 무단 상대에게 권한을 줄 수 있지만, maker의 가격·수량·수취인·만기를 바꾸거나 일방 출금을 허용하지는 못해야 한다.

v1의 `fill`은 `msg.sender == permit.taker`여야 한다. relayer/제3자 gas 대납은 별도 명세 전까지 지원하지 않는다. 공개 calldata를 복사한 제3자는 자신의 주소로 수취인을 바꿀 수 없고, 원 taker 대신 호출할 수도 없다. A와 B 동일 주소는 거부한다. 임의 recipient 매개변수는 두지 않고 maker/taker 자신에게만 지급한다.

게이트 signer는 계약 생성자의 `GATE_SIGNER` 한 주소로 고정하고 maker 주문도 그 주소와 일치해야 한다. maker가 유효하게 서명했더라도 다른 gateSigner를 넣은 주문은 거부한다. 서버 설정 변경이 기존 계약의 signer를 바꾸지 않는다. 키 폐기/침해 시 기존 초대 발급 중단과 사용자 취소/만료, 새로운 signer를 고정한 **새 계약과 새 주문**으로 전환한다. 이미 발급된 permit은 온체인 취소 또는 자신의 만기까지 유효할 수 있다. 서버 키만으로 기존 주문을 다른 가격에 다시 서명할 수 없어야 한다. PIN pepper와 게이트 서명 키는 별도 비밀로 관리한다.

부분 체결 후에도 매번 permit이 필요하다. 첫 체결에서 boundTaker가 정해졌다고 게이트 검사를 면제하지 않는다. 잔액·승인과 거래 준비를 먼저 마치고 서명 요청 직전에 permit을 받는다. 만료된 permit을 재발급할 때는 서버가 현재 filledLots·초대 권한을 확인하고 새 nonce를 사용한다. 응답 재조회는 동일한 유효 permit을 반환할 수 있지만 만기를 몰래 연장하지 않는다. 제출한 tx의 결과가 불명확하면 먼저 receipt를 조회한다. 이 서버 발급/복구 연동은 출시 전 잔여 작업이다.

서버 발급의 필수 통과 조건(Fable R2 반영): 클라이언트가 보낸 `orderHash`를 그대로 서명하지 않는다. 저장된 초대 `terms`와 maker·방향·시장·단가·총수량·만기·부분 체결·NFT ID/모드를 정확히 대조하고, 전체 canonical Order 및 domain으로 hash를 서버에서 재계산한다. 주문 원문·maker 서명·nonce를 함께 보관하고 사용자가 내보낼 수 있어야 한다. 체인에서 filledLots·boundTaker·취소/epoch·잔액/승인을 고정 블록으로 다시 조회한 뒤 permit을 발급한다. `issuedAt`은 신선도가 확인된 블록 timestamp, `expiresAt`은 그 시각+300초와 주문 만기 중 작은 값으로 계산한다. 블록 시각은 벽시계와 다를 수 있고 reorg도 가능하므로 미래/정체/불일치한 블록에서는 발급을 중단한다. 조건이 다른 Order, 오래된 블록, 재조회로 만기 연장, 부분 체결 직후 stale expectedFilled 요청을 거부하는 서버 시험을 실제 연동의 필수 조건으로 둔다.

## 6. 최대 24시간, 취소, 승인 철회

- API 생성 시 `serverNow < expiresAt <= preparedAt + 86,400초`; 도중 서명해도 만기를 뒤로 미루지 않는다. 생성 challenge가 만료되면 새로 생성한다.
- 온체인은 `validAfter <= block.timestamp < expiresAt`, `0 < expiresAt-validAfter <= 86,400초`를 검사한다. 따라서 주문의 **실행 가능한 구간**이 최대 하루다. 오프체인 서명이 실제 생성된 시간을 블록체인이 증명한다는 뜻은 아니다. 미래 예약 시작은 UI/API에서 지원하지 않는다.
- 서버 시간은 접근 만기, 체인 timestamp는 체결 만기의 기준이다. 서명 직전 서버/체인 시각 차이와 최신 블록을 검사하고, 최신성이 불명확하면 새 정산 서명을 막는다. 클라이언트 시계만으로 연장하지 않는다.
- maker는 온체인 `cancel(Order order)` 또는 `cancelNonce(nonce)`, `cancelAll(newEpoch)`로 서버 없이 주문을 무효화할 수 있다. `cancel`은 `order.maker == msg.sender` 확인 후 canonical hash를 계산한다. 다른 두 함수는 호출자 자신의 nonce/epoch만 변경한다. `newEpoch`는 기존보다 커야 한다. 취소/체결 경쟁은 블록 내 먼저 확정된 상태가 우선한다. 취소 트랜잭션 hash만 얻은 상태는 ‘취소 대기’다.
- 복구 화면의 우선 경로는 `epoch(maker)` 조회 → `cancelAll(currentEpoch+1)`이며, **이 계약에서 해당 지갑의 기존 세대 주문 전체를 무효화**한다는 범위를 확인시킨다. 주문 원문을 잃어도 실행할 수 있다. nonce를 보관했다면 특정 `cancelNonce`를 제공하고, 원문을 보관했다면 `cancel(Order)`도 제공한다. 토큰 승인 철회는 별도 동작이다. 모두 사용자의 명시적 지갑 서명과 receipt 확인이 필요하다.
- 사이트에서 목록을 감추거나 서버 초대를 삭제하는 것은 온체인 취소가 아니다. 이미 발급된 permit도 만기까지 유효할 수 있다. 즉시 취소 보장은 온체인 확인 후 표시한다.
- 만기가 지나도 일반 ERC20 allowance는 자동으로 0이 되지 않는다. 주문 만료와 승인 만료를 분리하여 보여주고, 승인 철회 버튼을 제공한다. 승인 복구가 취소된 주문을 되살리지 않아야 한다.
- 비수탁 주문 만료에는 환불 트랜잭션이 없다. 미체결 자산이 계속 지갑에 있기 때문이다. **기존 예치 주문은 만기만으로 자동 출금되지 않는다.** 취소/만기 반환 함수 호출과, 지급 대기라면 withdraw가 필요하다. keeper 장애·발행자 동결은 24시간 제한만으로 해결되지 않는다.

## 7. 수량·가격·수수료 산식

v1은 기존 주문장의 검증 가능한 최소 단위를 재사용한다. ERC20 수량은 0.000001 토큰 단위, 가격은 0.000001 DAI/토큰 단위다. LGNS의 체인 decimals=9를 18로 바꾸지 않는다. 이보다 세밀한 입력은 **자동 자르거나 반올림하지 않고** 유효 단위를 안내한다. 접근 확인용 초안 API도 같은 6자리와 uint64 lots/ticks 한도를 검사하도록 보완했다. NFT 수량은 정수다. 실행 주문 생성 시에도 아래 단위와 초안의 정확한 일치를 검증해야 한다.

```text
d = token decimals (LGNS 9; AiLGNS/gLGNS 18)
lotSize = 10^d / 10^6  (LGNS 1,000 raw; 18-decimal token 10^12 raw)
baseRaw = fillLots * lotSize
unitPriceDaiWei = priceTicks * 10^12
grossDaiWei = fillLots * priceTicks * 10^6
feeDaiWei = grossDaiWei / 100
```

`lots`와 `priceTicks`는 양수 uint64이고 곱셈은 uint256에서 수행한다. 이 범위·스케일에서는 DAI gross와 1% fee가 정수로 정확히 나누어지므로 부분 체결마다 반올림 이득/손해가 생기지 않는다. `filledLots`로 누적 gross/fee를 계산하고 `newTotal - oldTotal`을 이번 지급액으로 사용해 접수증 합계를 대사한다. 향후 임의 raw 단위 허용은 별도 버전에서 누적 `ceil/floor` 방향·최종 dust 처리까지 다시 검증하며, 단순히 매 체결을 올림하면 손실이 없다는 주장은 금지한다.

| maker 주문 | 토큰 이동 | DAI 이동 | 수수료 부담 |
|---|---|---|---|
| 매도 | maker → taker: baseRaw | taker → maker: gross; taker → feeRecipient: fee | 구매하는 taker가 gross+fee 지불 |
| 매수 | taker → maker: baseRaw | maker → taker: gross-fee; maker → feeRecipient: fee | 판매하는 taker가 gross-fee 수령; maker 총 지출 gross |

feeRecipient가 당사자와 겹칠 수 있다. maker/taker 동일 주소는 금지하지만 수수료 수취 지갑의 정상 거래는 막지 않는다. 전송 계획을 토큰·주소별로 합산하고, 자기 자신에게 보내는 leg는 실행하지 않는다. 중복 주소를 제거한 전후 잔액의 **순변화량**이 계획과 같은지 대사한다. UI/접수증은 거래대금과 수수료 수입을 분리해 표시한다. 예를 들어 taker가 수수료 수취인이라면 본인이 낼 fee와 받을 fee가 상계되어 실제 지갑 순지출은 줄어든다. 이 중복 주소 4개 조합도 회귀 시험에 포함한다.

NFT는 1장 단위, 가격 tick=10^12 DAI wei, 수수료 역시 DAI 1%다. 분수 NFT를 만들지 않는다. v1 매도는 특정 tokenId 1장(`allowPartial=false`), 매수는 특정 tokenId 또는 사용자가 명시 동의한 동일 컬렉션의 카드 수량 입찰이다. 컬렉션 매수는 카드별 경제적 효용 동일성을 보장하지 않는다. HELIX Production NFT는 포함하지 않는다. 다장 매수 부분 체결은 1장씩, 각 카드 소유권·승인·잠금·recipient 안전 수신을 재검사한다.

## 8. 정산 절차와 자금 불변식

1. `nonReentrant`, chain/domain/deployment/시장 토큰·수수료 상수, maker 서명, gate 서명, 주문 상태/nonce/epoch/만기, taker/caller, 기존 boundTaker, 요청 수량·최소 부분 체결·잔량·permit의 expectedFilled를 확인한다.
2. 예상 지출·순수령을 계산한다. taker가 서명한 트랜잭션의 `maxDebit` 또는 `minNet`를 검사한다. maker가 받아야 하는 주소와 액수는 주문에서 고정된다.
3. 재진입 이전에 누적 체결·permit 소모·단일 taker 상태를 갱신한다(뒤 단계 실패 시 EVM revert로 복구). 외부 임의 callback/라우터 호출은 제공하지 않는다.
4. approved ERC20 두 자산의 양측 잔액을 읽고 `transferFrom`을 지갑→지갑으로 직접 호출한다. false 반환/비정상 반환/revert를 실패 처리한다. 한 번의 성공 반환만으로 세금 없는 전송이라고 판단하지 않는다.
5. maker/taker/수수료 주소의 전후 잔액 증감이 산식과 정확히 같은지 확인한다. 수취세·발신세·rebasing·restricted 때문에 다르면 전체 revert한다. 이 검사는 악의적으로 거짓 balanceOf를 반환하는 발행자 코드까지 신뢰 없게 만드는 증명은 아니다. 허용 토큰/구현 변경 감시가 필요하다.
6. NFT는 `safeTransferFrom` 및 전송 후 owner 검증, DAI의 exact-delta 검증을 함께 통과해야 한다. `isMounted` 같은 확인되지 않은 ABI를 가정하지 않고 실제 ABI·전송 `eth_call`을 이용한다.
7. 모든 이동과 검사가 성공한 뒤 체결 이벤트를 남긴다. 조회 화면은 체인 receipt와 이벤트·주문 누적 상태를 대조해 완료 표시한다.

불변식:

- 정상 주문 생성/취소/만료에 계약의 토큰 잔액이나 사용자 채무가 증가하지 않는다.
- 성공 체결은 양측 자산과 fee가 전부 맞으며, 부분 성공/지급 대기 채무를 허용하지 않는다. 실패 시 체결량·nonce·permit·잔액 변화가 모두 롤백된다(가스 지출 제외).
- 정산의 결과로 계약이 보유한 ERC20/NFT가 증가하지 않는다. 기부/강제 ETH/오입금 등 외부 전송은 이 불변식의 측정에서 별도로 분리한다. ‘잔고 항상 0’이라는 불가능한 전역 주장을 하지 않는다.
- 컨트랙트에 사용자의 자산을 임시 입금시켜 실패 지급을 `claimable`로 남기는 기존 경로를 재사용하지 않는다. fee 지급 실패도 전체 실패로 처리한다.
- maker 자산은 여러 오프체인 주문에 중복 약속될 수 있다. 이 주문은 확정 담보가 아니다. 지갑 잔액/승인·카드 잠금이 바뀌면 실행 불가로 표시하며, 체결 직전에도 다시 검증한다.

## 9. 지갑·RPC·장애 처리

- maker 주문과 gate permit 서명은 ECDSA로 검사한다(low-s, 잘못된 길이/v/zero signer 거부). ERC-1271의 유효 서명 응답을 검사하는 별도 경로는 구현하지 않았다. **taker는 직접 fill을 호출하는 계약 계정도 가능하다**(NFT receiver 테스트 포함). `msg.sender == taker`가 곧 EOA 전용이라는 뜻은 아니다. 특정 스마트지갑·ERC-4337·EIP-7702 제품의 전체 흐름 호환성은 별도 확인 없이는 표시하지 않는다. [ERC-1271](https://eips.ethereum.org/EIPS/eip-1271)
- 현재 초대 접근 게이트는 ECDSA `personal_sign` 확인(`verifyMessage`)만 구현하므로, 위의 **계약 수준 contract taker 가능**과 **제품 초대 경로 호환**은 다르다. ERC-1271 스마트 계정 접근은 미지원으로 표시한다. 7702 위임 EOA는 NFT 수신 시 위임 코드의 `onERC721Received` 호환성이 필요할 수 있으며 실제 지갑 조합 검증 전에는 지원 완료로 표시하지 않는다.
- EIP-7702 위임 EOA는 코드가 존재할 수 있다. `code.length > 0`만으로 일반 EOA 서명 불가라고 분류하지 않고 위임 표시/체인 기능/토큰의 전송 제한을 별도로 검증한다. 스마트지갑 전환이나 위임 해제를 자동으로 요구·실행하지 않는다. [EIP-7702](https://eips.ethereum.org/EIPS/eip-7702)
- WC는 연결 transport다. 접근 서명, 토큰 승인, maker 주문 서명, taker 정산 트랜잭션을 다른 단계로 표시한다. 현재 auth 전용 WC 세션에 나중에 무단으로 자산 전송 권한을 추가하지 않는다.
- 서울 RPC 두 곳 우선·보조 전환, 동일 블록/해시와 최신성을 확인한다. 가격 오라클은 비교 도움이며 서명한 고정 조건을 변경하지 않는다. 오라클 장애가 취소의 필요 조건이 되어서는 안 된다.
- 사이트/게이트가 내려가도 지갑 자산은 남아 있고 승인 철회·온체인 주문 취소는 독립 복구 페이지/탐색기 ABI로 가능해야 한다. 체인·전체 RPC 장애 동안 취소 트랜잭션 확정을 보장할 수는 없다.
- `eth_call` 성공은 그 블록에서의 실행 가능성이다. 채굴 전 잔액/승인/만기/선행 체결이 달라질 수 있어 실제 성공 보장이 아니다. 복귀한 탭은 저장된 tx hash를 재조회하며 응답 유실을 재전송으로 해결하지 않는다.
- 발행자가 지갑·정산 주소를 동결하거나 NFT 잠금을 바꾸는 위험은 남는다. 비수탁은 플랫폼 예치에 따른 장기 잠김을 줄이지만 발행자의 권한을 제거하지 않는다.

## 10. 구현·적대 검수의 통과 조건

| 영역 | 필수 공격/경계 시험 | 기대 결과 |
|---|---|---|
| PIN/링크 | 잘못된 secret, 전체 5회 실패, IP·지갑 교체, DO 재시작, 동시 unlock | 정보 비노출; 실패 누적 보존; 잠금 우회 불가 |
| 소유 확인 | wrong wallet/origin/chain, 변조 조건, nonce replay, 만료 서명, 두 지갑 동시 verify | 하나의 recipient만 결박; 조건 변조 실패 |
| WalletConnect | 앱 전환·탭 종료/복원·4001·만료·계정/체인 변경·중복 클릭 | 명시적 상태; 자동 재서명/자동 체결 금지 |
| 24시간 | API 경계 ±1초, 계약 만기와 같은 timestamp, 미래 시작/시계 역행 | 만기 이후 체결 불가; UI/API/체인 기준 구분 |
| 주문 서명 | 다른 체인/계약/토큰/price/fee/recipient/nonce/epoch | 모두 거부; 다른 배포에서 재사용 불가 |
| 단일 상대 | permit 다른 wallet/caller, calldata 복사, 첫 체결 이후 다른 taker | 자산 탈취/상대 교체 불가 |
| 부분 체결 | 1 lot·최대값·전량·잔량초과·최소수량·수백 회 분할·동시 fill | 누적 base/gross/fee가 전량과 정확히 일치 |
| 취소 경쟁 | 부분 체결 후 취소, cancelAll, 승인철회/복원, stale permit | 체인 상태 우선; 취소 복구 불가; 중복 체결 불가 |
| 전송 실패 | 발신/수신세, false/empty/malformed 반환, restricted, 가스 부족, fee 수취 실패, feeRecipient=당사자 4개 조합 | 정상 상계는 정확; 실제 실패는 전체 revert; 계약 보관/채무 0 증가 |
| NFT | mounted/소유자변경/승인철회/wrong tokenId/거부 receiver/콜백 재진입 | NFT와 DAI의 부분 교환 불가 |
| 게이트 침해 | 유효 signer가 임의 wallet/금액/만기/카드 permit 발행 | maker 주문 범위·고정 수취인·1%·onchain boundTaker를 넘지 못함 |
| 장애/오입금 | API 전체 중단, oracle stale, 공개 RPC 모두 실패, 강제 입금 | 자산이 정상 경로로 계약에 잠기지 않음; 잘못된 완료 표시 금지 |

정산 활성화 전: Solidity 단위/퍼즈/상태 불변식 시험 → 토큰별 고정 블록 `eth_call` → 안티그래비티의 독립 재현/적대 검수 → 보완 → 동일 소스 해시로 재검증 → manifest/runtime/source 확인 → 사용자 배포 서명 순서다. 테스트 통과를 무사고 보증으로 표현하지 않는다. 실기기/실제 자금 검증은 기존 합의대로 사용자에게 남긴다.

## 11. 범위와 남은 검증

신규 Solidity 계약과 실제 로컬 EVM 테스트가 추가되었다. 이는 배포, 시장 활성화, 실제 WC 기기 시험을 완료했다는 증거가 아니다. 화면에서 실행 가능한 범위는 여전히 **초대 생성·PIN 검사·지갑 접근 서명·거래 조건 화면 복구**다. NFT 초안의 tokenId 지정/소유 확인, maker EIP-712 주문 서명, approve, 운영 GatePermit 서명 키와 서버 발급, 실제 fill 연동은 필수 잔여 작업이다. 이들이 준비되지 않은 초안에서 결제·체결 버튼을 활성화하지 않는다.

`SUSPECT`: 지원 지갑별 Anubis WC·EIP-712·EIP-7702 조합, 발행자 전송 제한의 향후 변경, 새 정산 코드의 실제 가스/전송 호환성, 독립 노드 가용성. 현재 계약의 남은 예치금·지급 대기금은 새 비수탁 계약이 회수하거나 자동 이전할 수 없다.

검토 증거: 위 2절 기존 계약 동작은 코드에서 읽어 확인했다. 이번 보완에서는 계약 컴파일·로컬 EVM 트랜잭션·읽기 전용 생성 eth_call을 실행했다. 명령·출력·정확한 소스 해시·Fable 토론·Antigravity 독립 재현 범위는 구현 증거 문서를 따른다. 실제 자금 트랜잭션이나 배포는 실행하지 않았다.
