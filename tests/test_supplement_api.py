from fastapi.testclient import TestClient
from app import main, store
import json, io, zipfile


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'test.sqlite'))
    return TestClient(main.app)


TARGET = 'INV-A-20B,TELEMETRY,INV-A,2026-10-15T10:20:00+08:00,60,kW,800,健康,,\n'
REFERENCE = 'REF-A2-10B,TELEMETRY,REF-A2,2026-10-15T10:10:00+08:00,100,kW,800,健康,,\n'
VOID_TEXT = 'INV-A-20,冲突两行均不可信\nA-CONFLICT,冲突两行均不可信\nREF-A2-10,故障态被补证替代\n'


def test_supplement_workbench_page(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    base = c.post('/api/runs', json={}).json()
    rid = base['manifest']['run_id']
    html = c.get('/supplement?run_id=' + rid)
    assert html.status_code == 200
    for token in ['补证状态', '受影响时段', '未定损失', '原始隔离记录', '补证锁定', '裁定剔除', '提交补证']:
        assert token in html.text
    assert '?run_id=' + rid in html.text


def test_supplement_api_records_base_and_result(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    base = c.post('/api/runs', json={}).json()
    rid = base['manifest']['run_id']
    result = c.post('/api/supplement', json={'base_run_id': rid, 'csv_text': TARGET + REFERENCE, 'void_text': VOID_TEXT})
    assert result.status_code == 200
    data = result.json()
    report = data['supplement']
    assert data['manifest']['base_run_id'] == rid
    assert report['base']['run_id'] == rid
    assert report['base']['parent_run_id'] == rid
    assert report['status'] == '完整'
    assert report['available_loss_kwh'] == report['official_loss_kwh'] == '26.6667'
    assert report['counts']['undetermined'] == 0
    assert data['balance']['loss_kwh'] == '26.6667'
    # 基准批的 3 条原始隔离仍在，封存沿用原口径，故本批不可归档。
    assert data['archive']['status'] == '待复核'
    assert data['archive']['quarantined'] == 3
    # 基准批本身不受影响。
    stored = c.get('/api/runs/' + rid).json()
    assert stored['supplement']['official_loss_kwh'] == '13.3333'
    assert stored['supplement']['counts']['undetermined'] == 2


def test_supplement_api_is_idempotent(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={}).json()['manifest']['run_id']
    body = {'base_run_id': rid, 'csv_text': TARGET, 'void_text': VOID_TEXT}
    first = c.post('/api/supplement', json=body).json()
    second = c.post('/api/supplement', json=body).json()
    assert first['manifest']['run_id'] == second['manifest']['run_id']
    assert first['manifest']['sha256'] == second['manifest']['sha256']
    assert len([r for r in c.get('/api/runs').json() if r['id'] == first['manifest']['run_id']]) == 1


def test_supplement_rerun_preserves_values(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={}).json()['manifest']['run_id']
    created = c.post('/api/supplement', json={'base_run_id': rid, 'csv_text': TARGET + REFERENCE, 'void_text': VOID_TEXT}).json()
    sid = created['manifest']['run_id']
    rerun = c.post('/api/runs/' + sid + '/rerun', json={})
    assert rerun.status_code == 200
    data = rerun.json()
    assert data['manifest']['run_id'] == sid
    assert data['manifest']['sha256'] == created['manifest']['sha256']
    assert data['supplement']['official_loss_kwh'] == '26.6667'
    assert data['supplement']['immutability']['held'] == 0


def test_supplement_chaining_endpoint(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={}).json()['manifest']['run_id']
    first = c.post('/api/supplement', json={'base_run_id': rid, 'csv_text': REFERENCE}).json()
    second = c.post('/api/supplement', json={'base_run_id': first['manifest']['run_id'], 'csv_text': TARGET, 'void_text': VOID_TEXT}).json()
    report = second['supplement']
    assert report['base']['rounds'] == 2
    assert report['base']['parent_run_id'] == first['manifest']['run_id']
    assert report['base']['run_id'] == rid
    assert report['official_loss_kwh'] == '26.6667'


def test_supplement_requires_base(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    assert c.post('/api/supplement', json={'csv_text': TARGET}).status_code == 422
    assert c.post('/api/supplement', json={'base_run_id': 'none', 'csv_text': TARGET}).status_code == 404
    rid = c.post('/api/runs', json={}).json()['manifest']['run_id']
    assert c.post('/api/supplement', json={'base_run_id': rid}).status_code == 422
    assert c.post('/api/supplement', json={'base_run_id': rid, 'csv_text': TARGET, 'void': ['x']}).status_code == 422


def test_supplement_export_includes_workbench(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={}).json()['manifest']['run_id']
    created = c.post('/api/supplement', json={'base_run_id': rid, 'csv_text': TARGET}).json()
    sid = created['manifest']['run_id']
    bundle = c.get('/api/export/' + sid)
    assert bundle.status_code == 200
    evidence = json.loads(zipfile.ZipFile(io.BytesIO(bundle.content)).read('evidence.json'))
    assert evidence['supplement']['status'] == created['supplement']['status']
    assert evidence['supplement']['windows'] == created['supplement']['windows']
    assert evidence['supplement']['counts']['quarantine'] >= 1
    assert evidence['voided'] == created['voided']


def test_provisional_values_not_shown_as_official(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={}).json()['manifest']['run_id']
    # 补齐本机与对照（并裁定故障对照），但同刻冲突未裁定：能算出损失，却只能列为暂定。
    data = c.post('/api/supplement', json={'base_run_id': rid, 'csv_text': TARGET + REFERENCE,
                                           'void_text': 'REF-A2-10,故障态被补证替代\n'}).json()
    report = data['supplement']
    assert report['available_loss_kwh'] == '20.0000'
    assert report['official_loss_kwh'] == '13.3333'
    assert report['provisional_loss_kwh'] == '6.6667'
    assert report['counts']['unresolved_windows'] == 1
    html = c.get('/supplement?run_id=' + data['manifest']['run_id']).text
    assert '未核清' in html and '暂定' in html
    assert '正式值只统计已核清时段的确定损失' in html


def test_supplement_ignores_unchanged_duplicate(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={}).json()['manifest']['run_id']
    duplicate = 'INV-A-00,TELEMETRY,INV-A,2026-10-15T10:00:00+08:00,60,kW,800,健康,,\n'
    data = c.post('/api/supplement', json={'base_run_id': rid, 'csv_text': duplicate}).json()
    assert data['supplement']['ignored'][0]['reason'] == 'DUPLICATE_NOOP'
    assert data['supplement']['official_loss_kwh'] == '13.3333'
