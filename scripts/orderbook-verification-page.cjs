const E = require('ethers');

function verificationPage({ deployment, block, coverage, fee, escrow }, staticSnapshot = false) {
  const local = deployment.chainId === 31337;
  const percent = bps => bps / 100 + '%';
  const date = seconds => new Date(seconds * 1000).toISOString();
  const tokens = Object.entries(escrow).map(([symbol, state]) => {
    const difference = BigInt(state.differenceWei);
    const status = difference === 0n ? '장부와 일치' : difference < 0n ? '잔액 부족' : '장부보다 잔액이 많음';
    const amount = key => E.formatEther(BigInt(state[key]));
    return `<h3>${symbol} · ${status}</h3><p>계약 잔액 ${amount('balanceWei')} · 주문 예치 ${amount('orderReserveWei')}
      · 지급 대기 ${amount('claimableWei')} · 차이 ${amount('differenceWei')}</p>`;
  }).join('');
  return `<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>수수료·예치금 검증</title><link rel="stylesheet" href="/app.css"></head>
<body><header class="masthead">
  ${staticSnapshot ? '<span>주문장 검증 기록</span>' : '<a href="/">거래 터미널</a>'}
  <span>${local ? '로컬 시험 · 모의 토큰' : '기준 블록 스냅샷'}</span>
</header>
<main class="verification">
  <h1>수수료·예치금 검증</h1>
  <p>이 주소를 AI에 공유해 아래 근거를 검토할 수 있습니다.${local ? ' 현재 기록은 로컬 시험 결과입니다.' : ''}</p>
  <section><h2>확인 대상</h2>
    <p>체인 ID ${deployment.chainId} · 조회 블록 ${block.number} · ${date(block.timestamp)}</p>
    <p>주문장 <code>${deployment.address}</code></p>
    <p>조회 블록 해시 <code>${block.hash}</code></p>
    <p>배포 거래 <code>${deployment.deployTx}</code></p>
    <p>런타임 코드 해시 <code>${deployment.runtimeKeccak}</code></p>
    <p>시험 AiLGNS <code>${deployment.tokenA}</code></p>
    <p>시험 DAI <code>${deployment.tokenB}</code></p>
  </section>
  <section><h2>수수료 변경 권한</h2>
    <p>현재 테이커 요율 ${percent(fee.activeBps)} · 계약상 최대 ${percent(fee.maxBps)}
      · 최소 지연 ${fee.delaySeconds}초</p>
    <p>메이커 0%. 테이커 수수료는 체결대금 기준 DAI로 정산합니다.</p>
    <p>수취 주소 <code>${fee.recipient}</code></p>
    <p>예약 권한 <code>${fee.authority}</code></p>
    <p>${fee.pending ? '예약 요율 ' + percent(fee.pending.bps) + ' · 실행 가능 시각 ' + date(fee.pending.executableAfter) : '예약된 변경 없음'}</p>
    <p>이미 접수된 주문은 당시 요율을 유지합니다. ${local ? '표시된 권한과 설정은 로컬 시험용입니다.' : '권한 주소의 다중서명 구성은 별도 확인 대상입니다.'}</p>
  </section>
  <section><h2>예치금 대사</h2>
    <p>검사한 주문 ${coverage.orders}건 · 대조한 지급 대기 이벤트 ${coverage.deferredPayouts}건</p>
    ${tokens}
    <p>만기가 지났어도 반환 전이면 주문 예치에 포함합니다. 이 결과는 표시된 한 블록의 잔액 대사입니다.</p>
  </section>
  <section><h2>검증 자료</h2>
    <ul>
      <li><a href="/verification.json${staticSnapshot ? '' : '?block=' + block.number}">이 블록의 대사와 배포 기록 JSON</a></li>
      <li><a href="/contract.sol">컨트랙트 전체 소스</a> · <a href="/abi.json">ABI</a> · <a href="/standard-input.json">컴파일 입력</a></li>
      <li><a href="/review.md">코드·권한·잔액 재현 절차</a>${staticSnapshot ? '' : ' · <a href="/ai-review.txt">AI 검토 질문과 소스</a>'}</li>
    </ul>
    <p>${local ? '운영 배포와 외부 감사는 별도 확인 대상입니다.' : '토큰 권한과 외부 감사는 이 대사 결과에 포함되지 않습니다.'}</p>
  </section>
</main></body></html>`;
}

module.exports = { verificationPage };
