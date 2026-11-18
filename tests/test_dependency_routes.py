"""Dependency transport is authenticated, target-bound and disabled by default."""
# ruff: noqa: F811 -- imported pytest fixtures are resolved by argument name

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import dependency_routes as routes
from lib.dependency_store import DependencyStore
from tests.test_dependency_runtime import Adapter, plan  # noqa: F401


@pytest.fixture
def client(tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(routes.router, prefix='/api')
    monkeypatch.setenv('RHDP_API_KEY', 'test-key')
    with DependencyStore(tmp_path/'dependencies.sqlite3') as store:
        app.state.dependency_store = store
        app.state.dependency_adapter = lambda target: Adapter()
        with TestClient(app) as value:
            yield value


def test_disabled_capability_and_staging(client, plan):
    headers = {'X-API-Key': 'test-key'}
    assert client.get('/api/capabilities', headers=headers).json()['deployment_contracts'] == []
    assert client.post('/api/dependency-plans', json=plan.model_dump(mode='json'), headers=headers).status_code == 503


def test_auth_required_on_reads_and_mutations(client, plan, monkeypatch):
    monkeypatch.setattr(routes, 'RUNTIME_READY', True)
    assert client.get('/api/capabilities').status_code == 403
    assert client.post('/api/dependency-plans', json=plan.model_dump(mode='json')).status_code == 403


def test_staging_idempotency_target_authorization_and_safe_errors(client, plan, monkeypatch):
    monkeypatch.setattr(routes, 'RUNTIME_READY', True)
    headers = {'X-API-Key': 'test-key', 'X-RHDP-Target-Cluster': 'events'}
    url = '/api/dependency-plans'
    payload = plan.model_dump(mode='json')
    assert client.post(url, json=payload, headers=headers).status_code == 200
    assert client.post(f'{url}/run-1/runs', headers=headers).status_code == 422
    headers['Idempotency-Key'] = 'submission-1'
    assert client.post(f'{url}/run-1/runs', headers=headers).status_code == 200
    assert client.post(f'{url}/run-1/runs', headers=headers).status_code == 200
    assert client.get('/api/dependency-runs/run-1', headers=headers).status_code == 200
    monkeypatch.setenv('DEPLOY_PICKER_ALLOWED_EMAILS', 'operator@example.com')
    headers['X-RHDP-Target-Cluster'] = 'other'
    assert client.get('/api/dependency-runs/run-1', headers=headers).status_code == 403
    headers['X-RHDP-Target-Cluster'] = 'events'
    payload['nodes'][1]['parameters']['password'] = 'synthetic-secret'
    response = client.post(url, json=payload, headers=headers)
    assert response.status_code == 422
    assert 'synthetic-secret' not in response.text


def test_csv_rejects_dependency_markers(tmp_path):
    from rhdp_flow import read_csv_input
    from tests.conftest import BASIC_WORKSHOP_CSV
    lines = BASIC_WORKSHOP_CSV.splitlines()
    path = tmp_path/'dependency.csv'
    path.write_text(lines[0]+',Dependency_Plan\n'+lines[1]+',parent_child_v1\n')
    with pytest.raises(ValueError, match='dependency_plan_requires_structured_transport'):
        read_csv_input(str(path))


def test_staging_and_runs_refuse_implicit_default_target(client, plan, monkeypatch):
    from api import identity
    monkeypatch.setattr(routes, 'RUNTIME_READY', True)
    monkeypatch.setattr(identity, 'effective_target', lambda requested: 'events')
    headers = {'X-API-Key': 'test-key'}
    payload = plan.model_dump(mode='json')
    response = client.post('/api/dependency-plans', json=payload, headers=headers)
    assert response.status_code == 422
    assert response.json()['detail'] == 'explicit_dependency_target_required'
    headers['X-RHDP-Target-Cluster'] = 'events'
    assert client.post('/api/dependency-plans', json=payload, headers=headers).status_code == 200
    headers.pop('X-RHDP-Target-Cluster')
    headers['Idempotency-Key'] = 'run-1'
    assert client.post('/api/dependency-plans/run-1/runs', headers=headers).status_code == 422
