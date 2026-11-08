"""Compile in-memory negative mutations; never change product sources/artifacts.

Success means each intended accounting/authorization break is caught by an
assertion in an existing EVM test, not a compiler error or failed test setup.
"""
import hashlib
import io
import json
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'test'))
from test_escrow import ART
from test_noncustodial_settlement import TestDirectSettlement
from test_settlement_adversarial import TestSettlementAdversarial


def main():
    source_path = 'contracts/NoncustodialSettlement.sol'
    source = (ROOT / source_path).read_text()
    compiler_input = json.loads((ROOT / 'build/NoncustodialSettlement.standard-input.json').read_text())
    assert compiler_input['sources'][source_path]['content'] == source, 'Stale standard input'
    original_artifact = ART['NoncustodialSettlement']
    rows = []
    cases = [
        ('fee_halved', 'x.fee = newGross/100-oldGross/100;', 'x.fee = newGross/200-oldGross/200;',
         TestDirectSettlement, 'test_partial_fill_cancel_leaves_all_unfilled_assets_in_wallet'),
        ('base_delta_check_removed', '_verify(BASE_TOKEN,b.parties,b.baseBefore,b.baseCredit,b.baseDebit);', '',
         TestSettlementAdversarial, 'test_final_fee_callback_cannot_change_already_transferred_base_token'),
        ('nonce_cancel_ignored', 'cancelledNonce[o.maker][o.makerNonce] ||', 'false ||',
         TestDirectSettlement, 'test_cancel_nonce_and_epoch_do_not_require_gate_or_oracle'),
    ]
    for name, needle, replacement, case, method in cases:
        assert source.count(needle) == 1, f'Mutation anchor drift: {name}'
        mutated = source.replace(needle, replacement, 1)
        compiler_input['sources'][source_path]['content'] = mutated
        build = subprocess.run(['node', 'scripts/solc.cjs'], cwd=ROOT, input=json.dumps(compiler_input),
                               text=True, capture_output=True, check=True)
        output = json.loads(build.stdout)
        errors = [e for e in output.get('errors', []) if e['severity'] == 'error']
        assert not errors, errors
        compiled = output['contracts'][source_path]['NoncustodialSettlement']
        ART['NoncustodialSettlement'] = {**original_artifact, 'abi': compiled['abi'], 'bin': compiled['evm']['bytecode']['object']}
        log = io.StringIO()
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(unittest.TestSuite([case(method)]))
        detected = len(result.failures) == 1 and not result.errors and result.testsRun == 1
        rows.append({'mutation': name, 'test': method, 'detected': detected, 'failures': len(result.failures),
                     'errors': len(result.errors), 'output': log.getvalue()})
        print(f'{name}: {"DETECTED" if detected else "INVALID_OR_MISSED"} (assertion failures={len(result.failures)}, errors={len(result.errors)})')
        ART['NoncustodialSettlement'] = original_artifact
    assert (ROOT / source_path).read_text() == source, 'Product source unexpectedly changed'
    report = {'sourceSha256': hashlib.sha256(source.encode()).hexdigest(), 'mutations': rows,
              'scope': 'Three selected negative mutations; not exhaustive mutation coverage.'}
    (ROOT / 'build/noncustodial-mutations.json').write_text(json.dumps(report, indent=2) + '\n')
    assert all(row['detected'] for row in rows), 'Some mutations were not detected by assertions'


if __name__ == '__main__':
    main()
