"""HTTP contracts with an isolated database and no application lifespan or live model."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.api import assistants, memory_jobs as api_jobs
from server.db import repos, schema
from server.runtime import memory_jobs


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv('TIGEROSE_HOME', str(tmp_path))
    monkeypatch.setenv('AVENT_HOME', str(tmp_path))
    monkeypatch.setattr('avent_paths._DATA_ROOT', tmp_path)
    schema.init_db()
    owner = repos.create_template(name='Memory integration test')['template_id']
    app = FastAPI()
    app.include_router(assistants.router)
    app.include_router(api_jobs.router)
    client = TestClient(app)
    yield client, '/api/assistants/' + owner, owner, tmp_path
    client.close()


def create_memory(client, base, text='供应商文档 https://example.com/docs'):
    response = client.post(base + '/memories/confirm', json={
        'body': text, 'type': 'reference', 'summary': '供应商接口文档',
        'scope_kind': 'assistant', 'source': 'manual'})
    assert response.status_code == 200, response.text
    return response.json()


def queue(owner, source='run'):
    conn = repos.get_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO session_fences (session_id,epoch) VALUES ('api-session',0)")
        job = memory_jobs.enqueue_completed(owner, 'api-session', 0, source, [
            {'role': 'user', 'content': 'Use this supplier', 'run_id': source,
             'source_kind': 'message', 'source_id': 'original-user-message'},
            {'role': 'user', 'content': 'API address https://example.com/docs', 'run_id': source,
             'source_kind': 'question', 'source_id': 'original-card', 'question_id': 'original-card'},
            {'role': 'assistant', 'content': 'Verified and complete'}], conn=conn)
        conn.commit()
        return job
    finally:
        conn.close()


def run_worker(proposals):
    worker = memory_jobs.MemoryWorker(repos.get_connection, lambda *args: proposals)
    assert worker.run_once()


def test_structured_crud_version_conflict_and_revision_restore(api):
    client, base, owner, _ = api
    memory = create_memory(client, base)
    assert memory['version'] == 1
    assert memory['scope_id'] == owner
    assert isinstance(memory['source_refs'], list)
    assert isinstance(memory['updated_at'], (int, float))
    path = base + '/memories/' + memory['memory_id']
    updated = client.patch(path, json={'body': 'New endpoint https://example.com/v2', 'expected_version': 1})
    assert updated.status_code == 200
    assert updated.json()['version'] == 2
    conflict = client.patch(path, json={'body': 'Stale replacement', 'expected_version': 1})
    assert conflict.status_code == 409
    assert conflict.json()['detail']['current_version'] == 2
    revisions = client.get(path + '/revisions').json()
    assert [(r['version'], r['after']['version']) for r in revisions] == [(2, 2), (1, 1)]
    assert all({'memory_id', 'version', 'before', 'after', 'actor', 'ts'} <= r.keys() for r in revisions)
    assert client.delete(path + '?expected_version=2').status_code == 200
    assert client.get(base + '/memories').json() == []
    restored = client.post(path + '/restore', json={'revision_version': 1, 'expected_version': 3})
    assert restored.status_code == 200
    assert restored.json()['version'] == 4
    assert restored.json()['body'] == memory['body']
    assert client.get(base + '/memories?type=reference&scope_kind=assistant').json()[0]['memory_id'] == memory['memory_id']


def test_candidate_confirmation_dismissal_and_source_contract(api):
    client, base, owner, _ = api
    queue(owner)
    run_worker([{'body': 'Preferred vendor https://example.com/docs', 'type': 'reference'},
                {'body': 'User prefers concise replies', 'type': 'user'}])
    candidates = client.get(base + '/memory-candidates')
    assert candidates.status_code == 200
    rows = candidates.json()
    assert len(rows) == 2
    required = {'candidate_id', 'job_id', 'body', 'status', 'before_body', 'source_refs'}
    assert all(required <= row.keys() for row in rows)
    assert {r['kind'] for r in rows[0]['source_refs']} >= {'memory_job', 'message', 'question'}
    assert client.get(base + '/memories').json() == []
    confirmed = client.post(base + '/memory-candidates/' + rows[0]['candidate_id'] + '/confirm')
    assert confirmed.status_code == 200
    assert confirmed.json()['status'] == 'confirmed'
    assert client.post(base + '/memory-candidates/' + rows[0]['candidate_id'] + '/confirm').json()['memory_id'] == confirmed.json()['memory_id']
    dismissed = client.post(base + '/memory-candidates/' + rows[1]['candidate_id'] + '/dismiss')
    assert dismissed.status_code == 200
    assert dismissed.json()['status'] == 'dismissed'
    assert len(client.get(base + '/memories').json()) == 1
    jobs = client.get(base + '/memory-jobs').json()
    assert {'job_id', 'kind', 'status', 'attempts', 'error'} <= jobs[0].keys()
    assert jobs[0]['status'] == 'completed'


def test_organize_candidate_conflict_is_http409_and_does_not_overwrite(api):
    client, base, owner, _ = api
    memory = create_memory(client, base)
    response = client.post(base + '/memories/organize', json={
        'scope_kind': 'assistant', 'scope_id': owner, 'memory_ids': [memory['memory_id']]})
    assert response.status_code == 200, response.text
    run_worker([{'intent': 'update', 'target_memory_id': memory['memory_id'], 'body': 'Consolidated description'}])
    candidate = client.get(base + '/memory-candidates').json()[0]
    assert candidate['before_body'] == memory['body']
    assert candidate['target_version'] == 1
    client.patch(base + '/memories/' + memory['memory_id'], json={'body': 'User edited after proposal', 'expected_version': 1})
    response = client.post(base + '/memory-candidates/' + candidate['candidate_id'] + '/confirm')
    assert response.status_code == 409
    assert response.json()['detail']['status'] == 'conflict'
    assert client.get(base + '/memories').json()[0]['body'] == 'User edited after proposal'


def test_import_preview_confirmation_idempotency_and_swift_fields(api):
    client, base, owner, tmp_path = api
    source = tmp_path / 'selected-cli-memory'
    source.mkdir()
    (source / 'vendor.md').write_text('---\ntype: reference\nscope: project\n---\nVendor https://example.com/api', encoding='utf-8')
    request = {'path': str(source), 'scope_kind': 'assistant', 'scope_id': owner}
    preview = client.post(base + '/memories/import-preview', json=request)
    assert preview.status_code == 200, preview.text
    value = preview.json()
    assert {'preview_id', 'entries'} <= value.keys()
    assert {'body', 'type', 'summary', 'source_path', 'source_scope', 'content_hash'} <= value['entries'][0].keys()
    assert client.get(base + '/memories').json() == []
    payload = {'preview_id': value['preview_id']}
    first = client.post(base + '/memories/import-confirm', json=payload)
    assert first.status_code == 200
    assert client.post(base + '/memories/import-confirm', json=payload).json() == first.json()
    assert len(client.get(base + '/memories').json()) == 1


def test_candidate_source_clear_is_http409_and_scrubbed(api):
    client, base, owner, _ = api
    queue(owner)
    run_worker([{'body': 'Old source information', 'type': 'reference'}])
    candidate = client.get(base + '/memory-candidates').json()[0]
    conn = repos.get_connection()
    try:
        conn.execute("UPDATE session_fences SET epoch=1 WHERE session_id='api-session'")
        conn.commit()
    finally:
        conn.close()
    response = client.post(base + '/memory-candidates/' + candidate['candidate_id'] + '/confirm')
    assert response.status_code == 409
    assert response.json()['detail']['status'] == 'cancelled'
    assert response.json()['detail']['body'] == ''
    assert client.get(base + '/memories').json() == []
    assert client.get(base + '/memory-candidates').json()[0]['body'] == ''


def test_maintenance_routes_require_existing_assistant(api):
    client, _, _, _ = api
    assert client.get('/api/assistants/missing/memory-jobs').status_code == 404
    assert client.get('/api/assistants/missing/memory-candidates').status_code == 404


def test_failed_maintenance_job_retries_through_http(api):
    client, base, owner, _ = api
    job = queue(owner)
    def fail(*args):
        raise TimeoutError('simulated provider timeout')
    assert memory_jobs.MemoryWorker(repos.get_connection, fail).run_once()
    assert client.get(base + '/memory-jobs').json()[0]['status'] == 'failed'
    response = client.post(base + '/memory-jobs/' + job['job_id'] + '/retry')
    assert response.status_code == 200
    run_worker([])
    state = client.get(base + '/memory-jobs').json()[0]
    assert (state['status'], state['attempts']) == ('completed', 2)
