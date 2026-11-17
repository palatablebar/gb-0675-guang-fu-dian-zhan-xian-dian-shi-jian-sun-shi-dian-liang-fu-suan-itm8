from importlib import import_module
from pathlib import Path
from copy import deepcopy
import json, time, base64
from app.config import ROOT, MODULES, MAX_BYTES
from app.errors import BusinessError
from app.manifest import digest, make_manifest
from app import supplement

MAX_ROUNDS = 20


def _base_context(snapshot):
    return deepcopy(snapshot['context']) if snapshot is not None else json.loads((ROOT/'sample_data/context.json').read_text())


def _void_map(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BusinessError('BAD_VOID', '补证裁定须为对象')
    return {str(k).strip(): str(v).strip() for k, v in value.items() if str(k).strip()}


def void_text_map(value):
    """每行一条“编号,原因”，用于页面或 API 提交裁定。"""
    if value is None or not isinstance(value, str):
        return {}
    out = {}
    for line in value.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, _, reason = line.partition(',')
        if key.strip():
            out[key.strip()] = reason.strip() or '人工裁定'
    return out


def _merge_void(*maps):
    merged = {}
    for item in maps:
        merged.update(item or {})
    return merged


def supplement_snapshot(base_result, csv_text, void):
    """在基准批（或上一轮补证批）之上追加一轮补证，返回不可变快照。"""
    if not isinstance(csv_text, str):
        raise BusinessError('BAD_SUPPLEMENT', '补证内容须为文本')
    parent = base_result['input_snapshot']
    if 'rounds' in parent:
        rounds = deepcopy(parent['rounds'])
        base_snapshot = parent['base_snapshot']
        root_source = parent.get('root_source', base_result.get('source', 'dirty'))
        base_run_id = parent.get('base_run_id', '')
    else:
        rounds = []
        base_snapshot = parent
        root_source = parent.get('root_source', base_result.get('source', 'dirty'))
        base_run_id = base_result['manifest']['run_id']
    if len(rounds) >= MAX_ROUNDS:
        raise BusinessError('TOO_MANY_ROUNDS', f'补证轮次不得超过{MAX_ROUNDS}轮，请合并后提交')
    rounds.append({'csv_b64': base64.b64encode(csv_text.encode()).decode(),
                   'void': _void_map(void), 'run_id': base_result['manifest']['run_id']})
    return {'bytes_b64': base_snapshot['bytes_b64'], 'base_snapshot': deepcopy(base_snapshot),
            'context': deepcopy(base_snapshot['context']), 'root_source': root_source,
            'base_run_id': base_run_id,
            'parent_run_id': base_result['manifest']['run_id'], 'rounds': rounds}


def evaluate_supplement(base_result, payload=None):
    payload = payload or {}
    csv_text = payload.get('csv_text', '')
    void = _merge_void(payload.get('void') or {}, void_text_map(payload.get('void_text')))
    if not isinstance(csv_text, str):
        raise BusinessError('BAD_SUPPLEMENT', '补证内容须为文本')
    if not csv_text.strip() and not _void_map(void):
        raise BusinessError('BAD_SUPPLEMENT', '请至少提交一条补传记录或一条裁定')
    snapshot = supplement_snapshot(base_result, csv_text, void)
    return evaluate({'dataset': snapshot['root_source']}, snapshot=snapshot)


def _run(state, until):
    for index, key in enumerate(MODULES, 1):
        if index > until:
            break
        import_module(f'app.modules.{key}.rules').apply(state)
    return state


def _new_state(raw, context, void, source):
    return {'raw_bytes': raw, 'context': context, 'records': [], 'quarantine': [], 'voided': [],
            'void': void, 'counts': {}, 'views': {}, 'source': source}


def _add_report(state, base_state, ledger, is_supplement):
    report = supplement.build(state, base_state, ledger)
    state['supplement'] = report
    if is_supplement:
        # 封存口径沿用原规则：无隔离、无未定片段、四账守恒，另需受影响时段全部核清。
        # 补证可以把未定补成确定，但不能替原始脏行开脱；确有坏行须重新导入基准批。
        b = state['balance']
        ready = (report['complete'] and not state['quarantine']
                 and b['row_balanced'] and b['time_balanced'] and b['energy_balanced']
                 and not report['counts']['unresolved_windows'])
        state['archive'] = {**state['archive'], 'status': '可归档' if ready else '待复核',
                            'unknown': b['unknown_segments'],
                            'unresolved': report['counts']['unresolved_windows'],
                            'quarantined': len(state['quarantine'])}
    state['views']['supplement'] = {'title': '补证工作台', 'rows': report['windows'],
        'metrics': {'可用损失_kWh': report['available_loss_kwh'], '正式损失_kWh': report['official_loss_kwh'],
                    '未定片段': report['counts']['undetermined'], '隔离记录': report['counts']['quarantine']}}
    return report


def _apply_round(raw, supplement_raw, previous, context, round_void, root_source):
    """应用一轮补证。若并集会改变上一轮已确定值（如迟到事件覆盖对照设备），
    把相关区域的补证行整组锁定后重算，保证批内不出现锁定冲突。"""
    hold_zones = set()
    merged, ledger, trial = raw, {'applied': [], 'held': [], 'ignored': [], 'submitted': 0}, None
    for _ in range(6):
        merged, ledger = supplement.prepare(raw, supplement_raw, previous, context, round_void, hold_zones)
        trial = _new_state(merged, deepcopy(context), dict(round_void), root_source)
        _run(trial, 7)
        violations = supplement.locked_violations(previous, trial)
        if not violations:
            return merged, ledger, trial
        zones = {segment['zone'] for segment in previous['energy_rows']
                 if (segment['device'], segment['start'], segment['end']) in set(violations)}
        new_zones = zones - hold_zones
        if not new_zones:
            break
        hold_zones |= new_zones
    return merged, ledger, trial


def _evaluate_supplement(snapshot, until, payload):
    """从基准批出发逐轮应用补证；每一轮的正式值成为下一轮的锁定边界。"""
    base_snapshot = snapshot['base_snapshot']
    root_source = snapshot.get('root_source', 'dirty')
    rounds = snapshot['rounds']
    if not isinstance(rounds, list) or not rounds:
        raise BusinessError('BAD_SUPPLEMENT', '缺少补证轮次')
    context = deepcopy(snapshot['context'])
    context.update(deepcopy(payload.get('overrides', {})))
    raw = base64.b64decode(base_snapshot['bytes_b64'], validate=True)
    base_state = evaluate({'dataset': root_source}, snapshot=base_snapshot)
    previous = base_state
    ledgers = []
    void = {}
    for index, item in enumerate(rounds):
        round_void = _void_map(item.get('void', {}))
        void = _merge_void(void, round_void)
        merged, ledger, mid = _apply_round(raw, base64.b64decode(item.get('csv_b64', '')),
                                           previous, context, round_void, root_source)
        ledgers.append(ledger)
        raw = merged
        if index + 1 < len(rounds):
            # 中间轮次按同一规则重放，其正式值作为下一轮不得改动的边界。
            cumulative = {'applied': [], 'held': [], 'ignored': [], 'submitted': 0}
            for number, item_ledger in enumerate(ledgers, 1):
                for key in ('applied', 'held', 'ignored'):
                    cumulative[key].extend({**row, 'round': number} for row in item_ledger.get(key, []))
                cumulative['submitted'] += item_ledger.get('submitted', 0)
            _add_report(mid, previous, cumulative, True)
            previous = mid
    return raw, context, void, root_source, base_state, previous, ledgers


def evaluate(payload=None, until=7, snapshot=None):
    payload = payload or {}
    dataset = payload.get('dataset', 'dirty')
    if dataset not in {'dirty', 'clean', 'legacy'}:
        raise BusinessError('BAD_DATASET', '样例名称无效')
    if not isinstance(payload.get('overrides', {}), dict):
        raise BusinessError('BAD_CONTEXT', '参数须为对象')
    ledgers = []
    base_state = None
    lock_state = None
    if snapshot is not None and 'base_snapshot' in snapshot and 'rounds' in snapshot:
        raw, context, void, dataset, base_state, lock_state, ledgers = _evaluate_supplement(snapshot, until, payload)
        context.update(deepcopy(payload.get('overrides', {})))
    else:
        if snapshot is not None and not isinstance(snapshot, dict):
            raise BusinessError('BAD_CONTEXT', '输入快照无效')
        raw = (base64.b64decode(snapshot['bytes_b64'], validate=True) if snapshot is not None
               else (str(payload['csv_text']).encode() if 'csv_text' in payload
                     else (ROOT/'sample_data'/f'{dataset}.csv').read_bytes()))
        context = _base_context(snapshot)
        context.update(deepcopy(payload.get('overrides', {})))
        void = _void_map(snapshot.get('void')) if snapshot is not None else {}
    if len(raw) > MAX_BYTES:
        raise BusinessError('TOO_LARGE', '文件超过2MB', 413)
    start = time.perf_counter()
    state = _new_state(raw, context, void, dataset)
    _run(state, until)
    is_supplement = bool(ledgers)
    if is_supplement:
        # 最后一轮之前已形成的正式值，本轮补证不得改变。
        supplement.verify_locked(lock_state, state)
    state['input_snapshot'] = {'bytes_b64': base64.b64encode(raw).decode(), 'context': deepcopy(context),
                               'void': deepcopy(void), 'root_source': dataset}
    if is_supplement:
        state['input_snapshot'].update({'base_snapshot': deepcopy(snapshot['base_snapshot']),
                                        'rounds': deepcopy(snapshot['rounds']),
                                        'base_run_id': snapshot.get('base_run_id', ''),
                                        'parent_run_id': snapshot.get('parent_run_id', '')})
    state.pop('raw_bytes', None)
    extra = ''
    if is_supplement:
        extra = json.dumps({'base': base_state['manifest']['sha256'],
                            'rounds': snapshot['rounds']}, ensure_ascii=False, sort_keys=True)
    state['manifest'] = make_manifest(digest(raw, context, extra), dataset, time.perf_counter() - start)
    if is_supplement:
        state['manifest']['base_run_id'] = snapshot.get('base_run_id', '')
    if until >= 7:
        total = {'applied': [], 'held': [], 'ignored': [], 'submitted': 0}
        for number, ledger in enumerate(ledgers, 1):
            for key in ('applied', 'held', 'ignored'):
                total[key].extend({**item, 'round': number} for item in ledger.get(key, []))
            total['submitted'] += ledger.get('submitted', 0)
        report = _add_report(state, base_state, total, is_supplement)
        report['base'] = {'run_id': snapshot.get('base_run_id', '') if is_supplement else state['manifest']['run_id'],
                          'parent_run_id': snapshot.get('parent_run_id', '') if is_supplement else '',
                          'source': dataset, 'is_self': not is_supplement,
                          'rounds': len(snapshot['rounds']) if is_supplement else 0}
    return state
