"""补证工作台：迟到补传的采纳、锁定、受影响时段与正式值判定。

核心约束：
1. 补证只能填补仍未定的时段；落在基准批已确定时段的补证行不并入，进入“补证锁定”台账。
2. 同一份补证与裁定重跑生成同一批号；已确定值按段位锁定，不新增、不覆盖、不改变。
3. 受影响时段未核清时，其中的片段只能列为暂定，不能计入正式损失。
4. 人工裁定（void）按证据编号将记录退出本次复算，原值原行保留在裁定台账，基准批本身不变。
"""
import csv
import hashlib
import io
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from app.coerce import instant, number
from app.errors import BusinessError

HEADER = ['id', 'kind', 'device', 'at', 'power', 'unit', 'irradiance', 'status', 'start', 'end']
ALIASES = {'编号': 'id'}
BASE_REASONS = {
    'TARGET_GAP': '本机遥测缺失或隔离',
    'TARGET_FAULT': '本机设备故障',
    'NO_REFERENCE': '合格对照不足',
    'ESTIMATED': '已拟合',
}
SOURCE_APPLIED = '补证采纳'
SOURCE_HELD = '补证锁定'
SOURCE_VOID = '裁定剔除'
SOURCE_QUARANTINE = '隔离'


def _decode(raw):
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise BusinessError('BAD_ENCODING', '补证文件编码无法识别')


def _lines(raw):
    if not raw:
        return []
    return [cells for cells in csv.reader(io.StringIO(_decode(raw), newline=''))
            if any(cell.strip() for cell in cells)]


def _header(cells):
    return [ALIASES.get(cell.strip(), cell.strip()) for cell in cells]


def _record(cells, header):
    return {key: value.strip() for key, value in zip(header, cells)}


def official_rows(base_state):
    """基准批中已成为正式值的片段；只有这些值受锁定保护。"""
    report = base_state.get('supplement') or {}
    status = {(s['device'], s['start'], s['end']): s['status']
              for s in report.get('segment_status', [])}
    out = []
    for row in base_state.get('energy_rows', []):
        if row['loss_kwh'] is None:
            continue
        key = (row['device'], row['start'], row['end'])
        if status and status.get(key) != '正式':
            continue
        out.append(row)
    return out


LOCKED_DETAIL = '补证记录落在已确定时段，未覆盖正式值'
ID_CONFLICT_DETAIL = '编号与基准批或本轮补证重复且取值不同，请先裁定原记录或改用新编号'


def _locked_reason(row, locked, context):
    """返回锁定原因；None 表示该补证行可以并入复算。

    已确定的正式时段整体锁定：任何迟到记录，无论本身是否合法，都不得改变其中取值，
    只登记到补证锁定台账；确需修改时须重新导入基准批。
    """
    try:
        if row.get('kind') == 'EVENT':
            start, end = instant(row['start']), instant(row['end'])
            for segment in locked:
                if segment['device'] == row.get('device') and instant(segment['start']) < end and start < instant(segment['end']):
                    return 'LOCKED_WINDOW'
            return None
        at = instant(row['at'])
        zone = context['devices'][row['device']]['zone']
    except (BusinessError, KeyError, TypeError):
        return None
    for segment in locked:
        if segment.get('zone') == zone and instant(segment['start']) <= at < instant(segment['end']):
            return 'LOCKED_WINDOW'
    return None


def _held_entry(row, reason, detail):
    return {'id': row.get('id', ''), 'kind': row.get('kind', ''), 'device': row.get('device', ''),
            'at': row.get('at', ''), 'start': row.get('start', ''), 'end': row.get('end', ''),
            'reason': reason, 'detail': detail, 'raw': dict(row)}


def prepare(base_raw, supplement_raw, base_state, context, void=None, hold_zones=None):
    """保留基准源全部原行，把可并入的补证行追加到源末尾。裁定由采信环节统一登记。

    hold_zones 用于回退：若某区域补证会改变已确定的正式值，该区域的补证行整组锁定。
    """
    void = void or {}
    hold_zones = set(hold_zones or ())
    locked = official_rows(base_state)
    base_lines = _lines(base_raw)
    if not base_lines:
        raise BusinessError('EMPTY_FILE', '基准源为空')
    header = _header(base_lines[0])
    if header != HEADER:
        raise BusinessError('BAD_HEADER', '基准源字段或顺序不匹配')
    base_ids = defaultdict(list)
    for cells in base_lines[1:]:
        if len(cells) != len(header):
            continue
        row = _record(cells, header)
        if row['id']:
            base_ids[row['id']].append(row)

    supplement_lines = _lines(supplement_raw)
    if supplement_lines:
        first = _header(supplement_lines[0])
        if first == HEADER:
            supplement_lines = supplement_lines[1:]  # 补证文件自带表头不算数据行
        elif set(first) & set(HEADER):
            raise BusinessError('BAD_HEADER', '补证字段或顺序与源数据不一致')
    counts = defaultdict(int)
    for cells in supplement_lines:
        if len(cells) == len(header) and cells[0].strip():
            counts[cells[0].strip()] += 1

    applied, held, ignored, batch_ids = [], [], [], set()
    for cells in supplement_lines:
        row = _record(cells, header)
        key = row.get('id', '')
        if len(cells) != len(header):
            # 列数不符的行交给采信环节隔离；但若仍能定位到已确定时段，一律锁定，不得影响正式值。
            if key not in void and _locked_reason(row, locked, context):
                held.append(_held_entry(row, 'LOCKED_WINDOW', LOCKED_DETAIL))
            else:
                applied.append(cells)
            continue
        if not key:
            # 无编号：随源进入采信，由采信环节隔离。
            applied.append(cells)
            continue
        if key in void:
            # 该编号本轮正被裁定剔除，补证行不能再沿用同一编号，否则会随原记录一并退出。
            batch_ids.add(key)
            ignored.append(_held_entry(row, 'VOID_CONFLICT', '该编号本轮已裁定剔除，请改用新编号补传'))
            continue
        if counts[key] > 1:
            held.append(_held_entry(row, 'ID_CONFLICT', ID_CONFLICT_DETAIL))
            continue
        existing = base_ids.get(key, [])
        same = [old for old in existing if all(old.get(k, '') == row.get(k, '') for k in HEADER)]
        if same:
            ignored.append(_held_entry(row, 'DUPLICATE_NOOP', '与基准批同编号同取值，不重复采信'))
            continue
        if existing:
            held.append(_held_entry(row, 'ID_CONFLICT', ID_CONFLICT_DETAIL))
            continue
        reason = _locked_reason(row, locked, context)
        if not reason and context['devices'].get(row.get('device'), {}).get('zone') in hold_zones:
            reason = 'LOCKED_WINDOW'
        if reason:
            held.append(_held_entry(row, reason, LOCKED_DETAIL))
            continue
        batch_ids.add(key)
        applied.append(cells)   # 只在先后顺序中保留第一行；重复编号已在上面整组挡下

    for key in void:
        if key not in base_ids and key not in batch_ids:
            ignored.append({'id': key, 'reason': 'VOID_UNKNOWN',
                            'detail': '裁定编号未在基准或补证中出现', 'raw': {}})

    merged = base_raw
    if applied:
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator='\n')
        writer.writerows(applied)
        text = _decode(base_raw).rstrip('\r\n') + '\n' + buffer.getvalue()
        merged = text.encode()
    ledger = {'applied': applied_rows(applied, header), 'held': held, 'ignored': ignored,
              'voided': [], 'submitted': len(applied) + len(held) + len(ignored)}
    return merged, ledger


def applied_rows(lines, header):
    return [_record(cells, header) for cells in lines if len(cells) == len(header)]


def _hits(segments, start, end, zone):
    found = []
    if zone is None:
        return found  # 设备无法归区（如设备编号不存在），无法归因到具体片段
    for segment in segments:
        if segment['zone'] != zone:
            continue
        if instant(segment['start']) < end and start < instant(segment['end']):
            found.append((segment['device'], segment['start'], segment['end']))
    return found


def _window_hits(state, ledger):
    cadence = state['context']['cadence_seconds']
    segments = state['energy_rows']
    hits = defaultdict(list)

    def add(rows, source, blocking):
        for row in rows:
            raw = row.get('raw') or {}
            device = raw.get('device', '')
            zone = state['context']['devices'].get(device, {}).get('zone')
            try:
                if raw.get('kind') == 'EVENT' and raw.get('start') and raw.get('end'):
                    start, end = instant(raw['start']), instant(raw['end'])
                elif raw.get('at'):
                    start = instant(raw['at'])
                    end = start + timedelta(seconds=cadence)
                else:
                    continue
            except BusinessError:
                continue
            code = row.get('code') or row.get('reason', '')
            record = {'source': source,
                      'blocking': blocking(code) if callable(blocking) else blocking,
                      'id': row.get('id', ''), 'code': code, 'detail': row.get('detail', ''),
                      'device': device, 'at': raw.get('at', ''),
                      'start': raw.get('start', ''), 'end': raw.get('end', '')}
            for key in _hits(segments, start, end, zone):
                hits[key].append(record)

    # 同值重复只是留痕，不影响时段可信度；其余隔离意味着该时段仍有未处理证据，
    # 时段内即便能算出取值也只能列为暂定。
    add(state['quarantine'], SOURCE_QUARANTINE, lambda code: code != 'SAMPLE_DUPLICATE')
    add(ledger.get('held', []), SOURCE_HELD, False)
    add(state.get('voided', []), SOURCE_VOID, False)
    add(ledger.get('applied', []), SOURCE_APPLIED, False)
    return hits


def _windows(state, hits):
    groups = []
    for segment in state['energy_rows']:
        key = (segment['device'], segment['start'], segment['end'])
        if key not in hits:
            continue
        if groups and groups[-1]['device'] == segment['device'] and groups[-1]['end'] == segment['start']:
            group = groups[-1]
            group['end'] = segment['end']
            group['seconds'] += segment['seconds']
            group['segments'].append(segment)
        else:
            groups.append({'device': segment['device'], 'start': segment['start'], 'end': segment['end'],
                           'seconds': segment['seconds'], 'segments': [segment]})
    windows = []
    for group in groups:
        seen, records = set(), []
        for segment in group['segments']:
            for record in hits[(group['device'], segment['start'], segment['end'])]:
                token = (record['source'], record['id'], record['code'])
                if token in seen:
                    continue
                seen.add(token)
                records.append(record)
        unknown = [segment for segment in group['segments'] if segment['loss_kwh'] is None]
        blocking = [record for record in records if record['blocking']]
        windows.append({'device': group['device'], 'start': group['start'], 'end': group['end'],
                        'seconds': group['seconds'], 'status': '未核清' if unknown or blocking else '已核清',
                        'records': records, 'unknown_segments': len(unknown),
                        'reasons': sorted({record['code'] for record in blocking if record['code']}),
                        'segments': group['segments']})
    return windows


def _sha(rows):
    payload = '\n'.join(sorted(
        f"{r['device']}|{r['start']}|{r['end']}|{r.get('actual_kw')}|{r.get('baseline_kw')}|{r.get('loss_kwh')}|{r.get('code')}"
        for r in rows))
    return hashlib.sha256(payload.encode()).hexdigest()


def build(state, base_state=None, ledger=None):
    ledger = ledger or {'applied': [], 'held': [], 'ignored': [], 'voided': [], 'submitted': 0}
    windows = _windows(state, _window_hits(state, ledger))
    unresolved = [window for window in windows if window['status'] != '已核清']
    unresolved_keys = {(s['device'], s['start'], s['end']) for w in unresolved for s in w['segments']}

    determined = [row for row in state['energy_rows'] if row['loss_kwh'] is not None]
    final_rows = [row for row in determined if (row['device'], row['start'], row['end']) not in unresolved_keys]
    provisional_rows = [row for row in determined if (row['device'], row['start'], row['end']) in unresolved_keys]
    quant = lambda rows: str(sum((number(row['loss_kwh']) for row in rows), Decimal(0)).quantize(Decimal('.0001')))

    undetermined = [{'device': row['device'], 'start': row['start'], 'end': row['end'],
                     'seconds': row['seconds'], 'code': row['code'],
                     'reason': BASE_REASONS.get(row['code'], row['code']),
                     'reference_ids': row.get('reference_ids', [])}
                    for row in state['energy_rows'] if row['loss_kwh'] is None]

    locked_scope = official_rows(base_state) if base_state else determined
    complete = not undetermined and not unresolved
    return {
        'mode': 'supplement' if (base_state is not None or ledger['submitted']) else 'base',
        'status': '完整' if complete else '待补证',
        'complete': complete,
        'loss_complete': not undetermined,
        'official_complete': not undetermined and not unresolved,
        'available_loss_kwh': quant(determined),
        'official_loss_kwh': quant(final_rows),
        'provisional_loss_kwh': quant(provisional_rows),
        'undetermined_kwh': None if undetermined else '0.0000',
        'counts': {'determined': len(determined), 'official': len(final_rows),
                   'provisional': len(provisional_rows), 'undetermined': len(undetermined),
                   'windows': len(windows), 'unresolved_windows': len(unresolved),
                   'quarantine': len(state['quarantine']), 'voided': len(state.get('voided', [])),
                   'held': len(ledger['held']), 'applied': len(ledger['applied']),
                   'ignored': len(ledger.get('ignored', [])), 'submitted': ledger['submitted']},
        'windows': [{k: v for k, v in window.items() if k != 'segments'} for window in windows],
        'segment_status': [{'device': row['device'], 'start': row['start'], 'end': row['end'],
                            'loss_kwh': row['loss_kwh'],
                            'status': ('未定' if row['loss_kwh'] is None else
                                       ('暂定' if (row['device'], row['start'], row['end']) in unresolved_keys else '正式'))}
                           for row in state['energy_rows']],
        'undetermined': undetermined,
        'quarantine': state['quarantine'],
        'voided': state.get('voided', []),
        'held': ledger['held'],
        'applied': ledger['applied'],
        'ignored': ledger.get('ignored', []),
        'immutability': {
            'locked_segments': len(locked_scope),
            'locked_loss_kwh': str(sum((number(row['loss_kwh']) for row in locked_scope), Decimal(0)).quantize(Decimal('.0001'))),
            'locked_sha': _sha(locked_scope),
            'determined_sha': _sha(determined),
            'held': len(ledger['held']),
        },
    }


def locked_violations(base_state, state):
    """列出被改动的正式片段；为空表示锁定成立。"""
    if 'energy_rows' not in state:
        return []
    before = {(r['device'], r['start'], r['end']): (r['actual_kw'], r['baseline_kw'], r['loss_kwh'], r['code'])
              for r in official_rows(base_state)}
    after = {(r['device'], r['start'], r['end']): (r['actual_kw'], r['baseline_kw'], r['loss_kwh'], r['code'])
             for r in state['energy_rows'] if r['loss_kwh'] is not None}
    changed = [key for key, value in before.items() if key in after and after[key] != value]
    missing = [key for key in before if key not in after]
    return changed + missing


def verify_locked(base_state, state):
    """兜底校验：正式值不得因补证而消失或改值。"""
    violations = locked_violations(base_state, state)
    if violations:
        detail = '；'.join(f"{key[0]} {key[1]}~{key[2]}" for key in violations[:5])
        raise BusinessError('LOCKED_VIOLATION', f'补证不得改变已确定值：{detail}')
