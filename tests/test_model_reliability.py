import json
import time

import httpx
import pytest
from sqlalchemy import select

from backend.platform_db import jobs, model_calls, model_stages, row, sessions
from backend.platform_jobs import ModelFailure, Worker, call_model, enqueue_periods
from backend.platform_models import ModelConfig
from test_platform import platform, user, device, seed, UI


def model_output(purpose):
    return '{"relationship":[],"profile":[]}' if '仅输出 JSON' in purpose else '合成结果'


def setup_job(platform, monkeypatch, *, daily_call_limit=100):
    client, user_id = user(platform)
    _, service, _ = platform
    sid, _ = seed(client, device(client))
    client.post(f'/api/v2/sessions/{sid}/listener', headers=UI, json={'enabled': True, 'cycles': ['day']})
    monkeypatch.setenv('WECHAT_MODEL_ALLOW_PRIVATE', '1')
    service.save_model(user_id, ModelConfig(base_url='http://127.0.0.1:9999/v1', model='test-model', daily_call_limit=daily_call_limit))
    with service.db.transaction() as con:
        job = con.execute(select(jobs).where(jobs.c.user_id == user_id)).mappings().one()
    return client, user_id, service, sid, dict(job)


def test_partial_success_is_checkpointed_across_safe_retry(platform, monkeypatch):
    client, user_id, service, _, job = setup_job(platform, monkeypatch)
    calls = []
    def model(config, prompt, purpose, before_call=None):
        before_call()
        calls.append(purpose)
        if len(calls) == 2:
            raise ModelFailure('限流', 'http_429', retryable=True, status=429, request_id='req-429')
        return model_output(purpose), {'prompt_tokens': 10, 'completion_tokens': 4, '_request_id': 'req-ok'}
    monkeypatch.setattr('backend.platform_jobs.call_model', model)
    assert Worker(service).run_once()
    # Changing only the daily limit must not invalidate paid stage results.
    service.save_model(user_id, ModelConfig(base_url='http://127.0.0.1:9999/v1', model='test-model', daily_call_limit=200))
    with service.db.transaction() as con:
        assert row(con, jobs, jobs.c.id == job['id'])['state'] == 'queued'
        con.execute(jobs.update().where(jobs.c.id == job['id']).values(next_run=0))
    assert Worker(service).run_once()
    with service.db.transaction() as con:
        assert row(con, jobs, jobs.c.id == job['id'])['state'] == 'done'
        records = list(con.execute(select(model_calls).where(model_calls.c.job_id == job['id'])).mappings())
        stages = list(con.execute(select(model_stages).where(model_stages.c.job_id == job['id'])).mappings())
    assert len(calls) == 3 and calls[1] == calls[2] and calls.count(calls[0]) == 1
    assert sorted(x['state'] for x in records) == ['retryable', 'success', 'success']
    assert len(stages) == 2 and all('合成结果' not in x['payload'] for x in stages)
    assert len(client.get('/api/v2/model/calls').json()['items']) == 3


def test_uncertain_timeout_does_not_replay_until_user_retries(platform, monkeypatch):
    client, user_id, service, sid, job = setup_job(platform, monkeypatch)
    calls = []
    def timeout(config, prompt, purpose, before_call=None):
        before_call(); calls.append(1)
        raise ModelFailure('请求超时，可能已计费', 'timeout', uncertain=True)
    monkeypatch.setattr('backend.platform_jobs.call_model', timeout)
    assert Worker(service).run_once()
    with service.db.transaction() as con:
        assert row(con, jobs, jobs.c.id == job['id'])['state'] == 'failed'
    # Incoming data and configuration saves do not silently reactivate failed work.
    with service.db.transaction() as con:
        enqueue_periods(con, user_id, row(con, sessions, sessions.c.id == sid), [1704067200], debounce=True)
    service.save_model(user_id, ModelConfig(base_url='http://127.0.0.1:9999/v1', model='test-model'))
    with service.db.transaction() as con:
        assert row(con, jobs, jobs.c.id == job['id'])['state'] == 'failed'
    assert not Worker(service).run_once() and len(calls) == 1
    assert client.post(f"/api/v2/jobs/{job['id']}/retry", headers=UI, json={}).status_code == 200
    monkeypatch.setattr('backend.platform_jobs.call_model', lambda config,prompt,purpose,before_call=None: (model_output(purpose), {}))
    assert Worker(service).run_once()
    with service.db.transaction() as con:
        assert row(con, jobs, jobs.c.id == job['id'])['state'] == 'done'
        assert con.execute(select(model_calls.c.state).where(model_calls.c.job_id == job['id'],
            model_calls.c.error_code == 'timeout')).scalar_one() == 'acknowledged'


def test_daily_limit_defers_and_resumes_from_checkpoint(platform, monkeypatch):
    client, user_id, service, _, job = setup_job(platform, monkeypatch, daily_call_limit=1)
    calls = []
    def model(config, prompt, purpose, before_call=None):
        before_call(); calls.append(purpose)
        return model_output(purpose), {'prompt_tokens': 2, 'completion_tokens': 3}
    monkeypatch.setattr('backend.platform_jobs.call_model', model)
    for day in range(2):
        assert Worker(service).run_once()
        with service.db.transaction() as con:
            state = row(con, jobs, jobs.c.id == job['id'])
            if day < 1:
                assert state['state'] == 'queued' and '今日模型调用上限' in state['error']
                assert state['next_run'] > time.time()
                con.execute(model_calls.update().where(model_calls.c.job_id == job['id']).values(started_at=0))
                con.execute(jobs.update().where(jobs.c.id == job['id']).values(next_run=0))
            else:
                assert state['state'] == 'done'
    assert len(calls) == 2 and len(set(calls)) == 2


def test_increasing_daily_limit_resumes_without_repeating_completed_stage(platform, monkeypatch):
    client, user_id, service, _, job = setup_job(platform, monkeypatch, daily_call_limit=1)
    calls = []
    def model(config, prompt, purpose, before_call=None):
        before_call(); calls.append(purpose)
        return model_output(purpose), {'prompt_tokens': 2, 'completion_tokens': 3}
    monkeypatch.setattr('backend.platform_jobs.call_model', model)
    assert Worker(service).run_once() and len(calls) == 1
    service.save_model(user_id, ModelConfig(base_url='http://127.0.0.1:9999/v1', model='test-model', daily_call_limit=3))
    with service.db.transaction() as con:
        assert row(con, jobs, jobs.c.id == job['id'])['next_run'] == 0
    assert Worker(service).run_once() and len(calls) == 2 and len(set(calls)) == 2
    with service.db.transaction() as con:
        assert row(con, jobs, jobs.c.id == job['id'])['state'] == 'done'


def test_provider_response_classification_and_thinking_parameters(monkeypatch):
    from backend import platform_jobs
    monkeypatch.setattr(platform_jobs, 'validate_model_url', lambda url: url)
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={'x-request-id': 'provider-123'}, json={
            'choices': [{'finish_reason': 'length', 'message': {'content': '不完整'}}],
            'usage': {'prompt_tokens': 5, 'completion_tokens': 7}})
    original = httpx.Client
    monkeypatch.setattr(platform_jobs.httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(handler)))
    config = {'configured': True, 'enabled': True, 'base_url': 'https://example.test/v1', 'model': 'test',
              'max_tokens': 50000, 'thinking_mode': 'enabled', 'thinking_budget': 512}
    with pytest.raises(ModelFailure) as error:
        call_model(config, '合成输入', '合成用途')
    assert error.value.code == 'output_truncated' and error.value.counts['completion_tokens'] == 7
    assert error.value.request_id == 'provider-123'
    assert requests[0]['max_tokens'] == 50000 and requests[0]['enable_thinking'] is True and requests[0]['thinking_budget'] == 512


@pytest.mark.parametrize('scenario,code,retryable,uncertain', [
    ('429', 'http_429', True, False), ('timeout', 'timeout', False, True),
    ('invalid', 'invalid_response', False, True), ('503', 'http_503', False, True),
])
def test_model_transport_errors_are_classified(monkeypatch, scenario, code, retryable, uncertain):
    from backend import platform_jobs
    monkeypatch.setattr(platform_jobs, 'validate_model_url', lambda url: url)
    def handler(request):
        if scenario == 'timeout': raise httpx.ReadTimeout('synthetic timeout', request=request)
        if scenario == 'invalid': return httpx.Response(200, content=b'invalid-json')
        return httpx.Response(int(scenario), headers={'x-request-id': 'synthetic-id'})
    original = httpx.Client
    monkeypatch.setattr(platform_jobs.httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(handler)))
    config = {'configured': True, 'enabled': True, 'base_url': 'https://example.test/v1', 'model': 'test'}
    with pytest.raises(ModelFailure) as error:
        call_model(config, '合成输入', '合成用途')
    assert (error.value.code, error.value.retryable, error.value.uncertain) == (code, retryable, uncertain)


def test_storage_failure_keeps_completed_stages_for_manual_resume(platform, monkeypatch):
    client, _, service, _, job = setup_job(platform, monkeypatch)
    calls = []
    def model(config, prompt, purpose, before_call=None):
        before_call(); calls.append(purpose)
        assert config['max_tokens'] == 2000
        return model_output(purpose), {'prompt_tokens': 10, 'completion_tokens': 3}
    monkeypatch.setattr('backend.platform_jobs.call_model', model)
    original_put = service.documents.put
    monkeypatch.setattr(service.documents, 'put', lambda *a: (_ for _ in ()).throw(OSError('storage failure')))
    assert Worker(service).run_once()
    with service.db.transaction() as con:
        assert row(con, jobs, jobs.c.id == job['id'])['state'] == 'failed'
    assert len(calls) == 2
    monkeypatch.setattr(service.documents, 'put', original_put)
    assert client.post(f"/api/v2/jobs/{job['id']}/retry", headers=UI, json={}).status_code == 200
    assert Worker(service).run_once()
    assert len(calls) == 2  # Storage recovery must not replay paid model stages.
    with service.db.transaction() as con:
        assert row(con, jobs, jobs.c.id == job['id'])['state'] == 'done'


def test_old_qwen_config_gets_safe_default_and_large_output_is_configurable(platform, monkeypatch):
    client, user_id, service, _, _ = setup_job(platform, monkeypatch)
    from backend.platform_db import models
    old = {'base_url': 'https://example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
           'model': 'qwen3.8-flash', 'api_key': 'test', 'enabled': True,
           'temperature': 0.3, 'max_tokens': 16384}
    with service.db.transaction() as con:
        con.execute(models.update().where(models.c.user_id == user_id).values(
            payload=service.vault.encrypt(user_id, 'model', user_id, old)))
    config = service.model_config(user_id, private=True)
    assert config['thinking_mode'] == 'disabled' and config['analysis_max_tokens'] == 2048
    assert config['daily_call_limit'] == 100
    assert ModelConfig(base_url=old['base_url'], model=old['model'], max_tokens=131072).max_tokens == 131072
    with pytest.raises(ValueError):
        ModelConfig(base_url=old['base_url'], model=old['model'], max_tokens=131073)


def test_unknown_response_record_after_crash_requires_manual_retry(platform, monkeypatch):
    client, user_id, service, _, job = setup_job(platform, monkeypatch)
    calls = []
    def model(config, prompt, purpose, before_call=None):
        before_call(); calls.append(1)
        return model_output(purpose), {'prompt_tokens': 5, 'completion_tokens': 5}
    monkeypatch.setattr('backend.platform_jobs.call_model', model)
    # Crash after provider success and usage accounting, before checkpoint persistence.
    original_encrypt = service.vault.encrypt
    def crash(user_id, kind, object_id, data):
        if kind == 'model_stages': raise OSError('checkpoint storage unavailable')
        return original_encrypt(user_id, kind, object_id, data)
    monkeypatch.setattr(service.vault, 'encrypt', crash)
    assert Worker(service).run_once()
    assert len(calls) == 1
    monkeypatch.setattr(service.vault, 'encrypt', original_encrypt)
    # A new worker must not unknowingly replay the already successful request.
    with service.db.transaction() as con:
        con.execute(jobs.update().where(jobs.c.id == job['id']).values(state='queued', next_run=0))
    assert Worker(service).run_once() and len(calls) == 1
    with service.db.transaction() as con:
        assert row(con, jobs, jobs.c.id == job['id'])['state'] == 'failed'
    assert client.post(f"/api/v2/jobs/{job['id']}/retry", headers=UI, json={}).status_code == 200
    assert Worker(service).run_once() and len(calls) == 3


def test_manual_test_calls_are_recorded_and_not_counted_as_automatic(platform, monkeypatch):
    client, user_id, service, _, job = setup_job(platform, monkeypatch, daily_call_limit=1)
    from backend.platform_jobs import tracked_direct_call
    calls = []
    def model(config, prompt, purpose, before_call=None):
        if before_call: before_call()
        calls.append(purpose)
        return '合成结果', {'prompt_tokens': 2, 'completion_tokens': 3, '_request_id': 'synthetic-request'}
    monkeypatch.setattr('backend.platform_jobs.call_model', model)
    config = service.model_config(user_id, private=True)
    tracked_direct_call(service, user_id, 'test', config, '合成测试', '测试用途')
    history = client.get('/api/v2/model/calls').json()['items']
    assert len(history) == 1 and history[0]['job_id'] == 'test' and history[0]['request_id'] == 'synthetic-request'
    other, _ = user(platform, 'other')
    assert other.get('/api/v2/model/calls').json()['items'] == []
    assert Worker(service).run_once()
    with service.db.transaction() as con:
        current = row(con, jobs, jobs.c.id == job['id'])
        assert current['state'] == 'queued' and '今日模型调用上限' in current['error']
    assert len(calls) == 2  # One explicit test plus one automatic call.
