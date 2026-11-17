from app.engine import evaluate, evaluate_supplement
from app.errors import BusinessError
import pytest

TARGET = 'INV-A-20B,TELEMETRY,INV-A,2026-10-15T10:20:00+08:00,60,kW,800,健康,,\n'
REFERENCE = 'REF-A2-10B,TELEMETRY,REF-A2,2026-10-15T10:10:00+08:00,100,kW,800,健康,,\n'
CONFLICT_VOID = {'INV-A-20': '冲突两行均不可信，采纳补证', 'A-CONFLICT': '冲突两行均不可信，采纳补证'}
REF_VOID = {'REF-A2-10': '故障态被同一时刻补证替代，采纳补证'}
FULL_VOID = {**CONFLICT_VOID, **REF_VOID}


def test_base_available_loss_and_unknown_split(sample):
    report = sample['supplement']
    assert report['status'] == '待补证'
    assert report['available_loss_kwh'] == '13.3333'
    assert report['official_loss_kwh'] == '13.3333'
    assert report['counts']['undetermined'] == 2
    assert {row['code'] for row in report['undetermined']} == {'NO_REFERENCE', 'TARGET_GAP'}


def test_base_quarantine_present_on_page_payload(sample):
    report = sample['supplement']
    codes = {row['code'] for row in report['quarantine']}
    assert 'SAMPLE_CONFLICT' in codes
    window = next(w for w in report['windows'] if w['device'] == 'INV-A' and w['start'].endswith('02:20:00+00:00'))
    assert window['status'] == '未核清'
    assert 'SAMPLE_CONFLICT' in window['reasons']


def test_filled_gap_without_resolving_conflict_stays_provisional(sample):
    # 补齐本机缺失与对照后损失能算出，但同刻冲突未裁定，该时段不能标为正式值。
    sup = evaluate_supplement(sample, {'csv_text': TARGET + REFERENCE, 'void': REF_VOID})
    report = sup['supplement']
    assert report['available_loss_kwh'] == '20.0000'
    assert report['official_loss_kwh'] == '13.3333'
    assert report['provisional_loss_kwh'] == '6.6667'
    assert report['counts']['unresolved_windows'] == 1
    assert report['status'] == '待补证'
    assert sup['archive']['status'] == '待复核'
    states = {(row['start'], row['status']) for row in report['segment_status']}
    assert ('2026-10-15T02:10:00+00:00', '暂定') in states


def test_full_supplement_forms_complete_result(sample):
    sup = evaluate_supplement(sample, {'csv_text': TARGET + REFERENCE, 'void': FULL_VOID})
    report = sup['supplement']
    assert report['status'] == '完整'
    assert report['complete'] is True
    assert report['official_loss_kwh'] == '26.6667'
    assert report['provisional_loss_kwh'] == '0.0000'
    assert report['counts']['undetermined'] == 0
    assert report['counts']['unresolved_windows'] == 0
    assert {row['status'] for row in report['segment_status']} == {'正式'}
    assert sup['balance']['loss_kwh'] == '26.6667'
    assert sup['balance']['row_balanced'] and sup['balance']['time_balanced'] and sup['balance']['energy_balanced']
    # 损失已完整可报；封存沿用原口径（无隔离），本批仍有 3 条原始隔离，故待复核。
    assert sup['archive']['status'] == '待复核'
    assert sup['archive']['quarantined'] == 3
    assert sup['supplement']['counts']['quarantine'] == 3


def test_quarantined_event_blocks_seal(clean):
    # 被隔离的限电事件可能意味着少算时段，即使损失、四账都齐也不能封存。
    broken_event = 'EB9,EVENT,INV-B,,,,,,2026-10-14T08:00:00+08:00,2026-10-15T09:00:00+08:00\n'
    sup = evaluate_supplement(clean, {'csv_text': broken_event})
    assert any(row['raw'].get('kind') == 'EVENT' for row in sup['quarantine'])
    assert sup['archive']['status'] == '待复核'
    assert sup['archive']['quarantined'] == 1


def test_complete_supplement_on_clean_base_is_sealable(clean):
    # 无隔离的基准批补入迟到记录后仍无隔离，补证完整且时段核清即可封存。
    extra = 'REF-B2-50X,TELEMETRY,REF-B2,2026-10-15T10:50:00+08:00,50,kW,800,健康,,\n'
    sup = evaluate_supplement(clean, {'csv_text': extra})
    report = sup['supplement']
    assert report['status'] == '完整'
    assert report['counts']['unresolved_windows'] == 0
    assert report['counts']['quarantine'] == 0
    assert report['official_loss_kwh'] == '26.6667'
    assert sup['archive']['status'] == '可归档'


def test_repeat_supplement_is_idempotent(sample):
    first = evaluate_supplement(sample, {'csv_text': TARGET + REFERENCE, 'void': FULL_VOID})
    again = evaluate_supplement(sample, {'csv_text': TARGET + REFERENCE, 'void': FULL_VOID})
    assert again['manifest']['run_id'] == first['manifest']['run_id']
    assert again['manifest']['sha256'] == first['manifest']['sha256']
    assert again['supplement'] == first['supplement']


def test_rerun_from_stored_snapshot_is_stable(sample):
    sup = evaluate_supplement(sample, {'csv_text': TARGET + REFERENCE, 'void': FULL_VOID})
    rerun = evaluate({'dataset': sup['source']}, snapshot=sup['input_snapshot'])
    assert rerun['manifest']['run_id'] == sup['manifest']['run_id']
    assert rerun['manifest']['sha256'] == sup['manifest']['sha256']
    assert rerun['supplement']['official_loss_kwh'] == '26.6667'


def test_supplement_cannot_overwrite_official_value(sample):
    locked_row = 'INV-A-00X,TELEMETRY,INV-A,2026-10-15T10:00:00+08:00,30,kW,800,健康,,\n'
    sup = evaluate_supplement(sample, {'csv_text': locked_row})
    report = sup['supplement']
    assert report['counts']['held'] == 1
    assert report['held'][0]['reason'] == 'LOCKED_WINDOW'
    assert report['applied'] == []
    assert report['official_loss_kwh'] == sample['supplement']['official_loss_kwh'] == '13.3333'
    assert report['immutability']['locked_sha'] == sample['supplement']['immutability']['determined_sha']
    assert 'INV-A-00X' not in {row['id'] for row in sup['records']}


def test_duplicate_supplement_row_not_added(sample):
    original = 'INV-A-00,TELEMETRY,INV-A,2026-10-15T10:00:00+08:00,60,kW,800,健康,,\n'
    sup = evaluate_supplement(sample, {'csv_text': original})
    report = sup['supplement']
    assert report['applied'] == []
    assert report['ignored'][0]['reason'] == 'DUPLICATE_NOOP'
    assert sup['balance']['input'] == sample['balance']['input']
    assert report['available_loss_kwh'] == sample['supplement']['available_loss_kwh']


def test_conflicting_id_held_not_applied(sample):
    clash = 'INV-A-00,TELEMETRY,INV-A,2026-10-15T10:00:00+08:00,70,kW,800,健康,,\n'
    report = evaluate_supplement(sample, {'csv_text': clash})['supplement']
    assert report['held'][0]['reason'] == 'ID_CONFLICT'
    assert report['applied'] == []
    assert report['official_loss_kwh'] == '13.3333'


def test_void_is_recorded_and_balanced(sample):
    sup = evaluate_supplement(sample, {'csv_text': TARGET, 'void': CONFLICT_VOID})
    assert sup['balance']['row_balanced'] is True
    assert {row['id'] for row in sup['voided']} == set(CONFLICT_VOID)
    assert not any(row['id'] in CONFLICT_VOID for row in sup['quarantine'])
    assert all(row['reason'] for row in sup['voided'])


def test_void_without_supplement_keeps_unknown(sample):
    # 裁定冲突后本机仍缺测，损失必须保持未定，不能填 0。
    sup = evaluate_supplement(sample, {'csv_text': '', 'void': CONFLICT_VOID})
    report = sup['supplement']
    assert report['official_loss_kwh'] == '13.3333'
    assert report['counts']['undetermined'] == 2
    assert sup['balance']['loss_kwh'] == '13.3333'


def test_second_round_on_supplement_batch(sample):
    first = evaluate_supplement(sample, {'csv_text': REFERENCE, 'void': REF_VOID})
    assert first['supplement']['provisional_loss_kwh'] == '6.6667'
    second = evaluate_supplement(first, {'csv_text': TARGET, 'void': CONFLICT_VOID})
    report = second['supplement']
    assert report['base']['parent_run_id'] == first['manifest']['run_id']
    assert report['base']['run_id'] == sample['manifest']['run_id']
    assert report['base']['rounds'] == 2
    assert report['official_loss_kwh'] == '26.6667'
    assert report['status'] == '完整'
    assert {row['round'] for row in report['applied']} == {1, 2}


def test_supplement_cannot_void_locked_value(sample):
    with pytest.raises(BusinessError) as error:
        evaluate_supplement(sample, {'csv_text': TARGET, 'void': {**CONFLICT_VOID, 'INV-A-00': '试图退回'}})
    assert error.value.code == 'LOCKED_VIOLATION'


def test_chained_round_override_rejected(sample):
    first = evaluate_supplement(sample, {'csv_text': REFERENCE + TARGET, 'void': FULL_VOID})
    later = evaluate_supplement(first, {'csv_text': 'INV-A-10X,TELEMETRY,INV-A,2026-10-15T10:10:00+08:00,20,kW,800,健康,,\n'})
    report = later['supplement']
    round_two_held = [row for row in report['held'] if row.get('round') == 2]
    assert [row['reason'] for row in round_two_held] == ['LOCKED_WINDOW']
    assert report['official_loss_kwh'] == first['supplement']['official_loss_kwh'] == '26.6667'


def test_second_round_can_fill_remaining_gap(sample):
    first = evaluate_supplement(sample, {'csv_text': REFERENCE, 'void': REF_VOID})
    second = evaluate_supplement(first, {'csv_text': TARGET, 'void': CONFLICT_VOID})
    # 第一轮已定的暂定值在第二轮转为正式，已正式的值保持不变。
    assert second['supplement']['counts']['undetermined'] == 0
    assert second['supplement']['official_loss_kwh'] == '26.6667'
    assert second['supplement']['status'] == '完整'
    rerun = evaluate({'dataset': second['source']}, snapshot=second['input_snapshot'])
    assert rerun['manifest']['run_id'] == second['manifest']['run_id']
    assert rerun['manifest']['sha256'] == second['manifest']['sha256']
    assert rerun['supplement'] == second['supplement']
    assert rerun['archive'] == second['archive']


def test_late_event_extension_keeps_locked_values(sample):
    # 迟到限电事件把 INV-B 并集延长到 10:30，不得改动 10:00–10:20 已定的两段。
    extension = ('EB2,EVENT,INV-B,,,,,,2026-10-15T10:20:00+08:00,2026-10-15T10:30:00+08:00\n'
                 'INV-B-20B,TELEMETRY,INV-B,2026-10-15T10:20:00+08:00,30,kW,800,健康,,\n'
                 'REF-B1-20B,TELEMETRY,REF-B1,2026-10-15T10:20:00+08:00,50,kW,800,健康,,\n'
                 'REF-B2-20B,TELEMETRY,REF-B2,2026-10-15T10:20:00+08:00,50,kW,800,健康,,\n')
    sup = evaluate_supplement(sample, {'csv_text': extension})
    report = sup['supplement']
    union = next(u for u in sup['event_unions'] if u['device'] == 'INV-B')
    assert union['seconds'] == 1800
    base_locked = {(r['device'], r['start']): r['loss_kwh'] for r in sample['energy_rows'] if r['loss_kwh']}
    for row in sup['energy_rows']:
        key = (row['device'], row['start'])
        if key in base_locked:
            assert row['loss_kwh'] == base_locked[key]
    inv_b = [row for row in report['segment_status'] if row['device'] == 'INV-B']
    assert [row['status'] for row in inv_b] == ['正式', '正式', '正式']
    assert report['held'] == []
    assert sup['balance']['time_balanced'] and sup['balance']['union_seconds'] == 3600


def test_invalid_late_record_in_locked_window_is_held(sample):
    # 已确定时段整体锁定：连非法迟到行也只登记锁定，不改动正式值。
    bad = 'BADX,TELEMETRY,INV-B,2026-10-15T10:10:00+08:00,50,W,800,健康,,\n'
    sup = evaluate_supplement(sample, {'csv_text': bad})
    report = sup['supplement']
    assert report['held'][0]['reason'] == 'LOCKED_WINDOW'
    assert report['official_loss_kwh'] == '13.3333'
    assert not any(row['id'] == 'BADX' for row in sup['quarantine'])


def test_replacement_uses_new_id_after_void(sample):
    # 被裁定的原记录由新编号的补传替代；同轮沿用旧编号会被拒绝以免歧义。
    sup = evaluate_supplement(sample, {'csv_text': REFERENCE, 'void': REF_VOID})
    assert [row['id'] for row in sup['supplement']['applied']] == ['REF-A2-10B']
    assert any(row['id'] == 'REF-A2-10' for row in sup['voided'])
    assert sup['balance']['row_balanced'] is True


def test_supplement_same_id_in_same_round_as_void_is_ignored(sample):
    # 本轮同时裁定剔除与补传同编号会造成歧义，直接拒绝并提示改用新编号。
    sup = evaluate_supplement(sample, {'csv_text': 'INV-A-20,TELEMETRY,INV-A,2026-10-15T10:20:00+08:00,60,kW,800,健康,,\n',
                                       'void': CONFLICT_VOID})
    reasons = {row['id']: row['reason'] for row in sup['supplement']['ignored']}
    assert reasons['INV-A-20'] == 'VOID_CONFLICT'
    assert sup['supplement']['official_loss_kwh'] == '13.3333'


def test_late_reference_curtailment_event_is_held_not_applied(sample):
    # 迟到事件若覆盖对照设备，会改变同区已确定的基线；必须整组锁定，不能悄悄改写正式值。
    late_event = 'EREF,EVENT,REF-B1,,,,,,2026-10-15T10:00:00+08:00,2026-10-15T10:20:00+08:00\n'
    sup = evaluate_supplement(sample, {'csv_text': late_event})
    report = sup['supplement']
    assert [row['reason'] for row in report['held']] == ['LOCKED_WINDOW']
    assert report['official_loss_kwh'] == sample['supplement']['official_loss_kwh'] == '13.3333'
    assert sup['balance']['loss_kwh'] == sample['balance']['loss_kwh']
    for row in sup['energy_rows']:
        original = next(r for r in sample['energy_rows']
                        if (r['device'], r['start'], r['end']) == (row['device'], row['start'], row['end']))
        if original['loss_kwh'] is not None:
            assert row['loss_kwh'] == original['loss_kwh']


def test_report_invariants_hold(sample):
    scenarios = [
        {'csv_text': TARGET},
        {'csv_text': REFERENCE, 'void': REF_VOID},
        {'csv_text': TARGET + REFERENCE, 'void': FULL_VOID},
        {'csv_text': REFERENCE + TARGET},
    ]
    from decimal import Decimal
    for payload in scenarios:
        sup = evaluate_supplement(sample, payload)
        report = sup['supplement']
        formal = {k for k, s in (((row['device'], row['start'], row['end']), row['status'])
                                 for row in report['segment_status']) if s == '正式'}
        unresolved = set()
        for window in report['windows']:
            if window['status'] != '已核清':
                for row in report['segment_status']:
                    if row['device'] == window['device'] and window['start'] <= row['start'] and row['end'] <= window['end']:
                        unresolved.add((row['device'], row['start'], row['end']))
        assert not (formal & unresolved), '未核清时段内不得出现正式值'
        parts = Decimal(report['official_loss_kwh']) + Decimal(report['provisional_loss_kwh'])
        assert abs(parts - Decimal(report['available_loss_kwh'])) <= Decimal('0.0001')
        if report['status'] == '完整':
            assert report['counts']['undetermined'] == 0
            assert report['counts']['unresolved_windows'] == 0
            assert report['provisional_loss_kwh'] == '0.0000'


def test_malformed_row_outside_locked_window_is_quarantined(sample):
    # 限电窗口之外的坏行不改变任何时段，仍按原口径隔离留痕。
    bad = 'EXTRA,TELEMETRY,INV-A,2026-10-15T13:00:00+08:00,60,kW,800,健康,,,extra\n'
    sup = evaluate_supplement(sample, {'csv_text': bad})
    assert any(row['id'] == 'EXTRA' and row['code'] == 'COLUMN_COUNT' for row in sup['quarantine'])
    assert sup['supplement']['official_loss_kwh'] == '13.3333'
    assert sup['balance']['row_balanced'] is True
