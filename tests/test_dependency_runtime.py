"""Hermetic dependency lifecycle tests; no cluster, credentials or live orders."""
import json
from datetime import UTC, datetime, timedelta

import pytest

from lib.dependency_contract import DependencyPlan
from lib.dependency_executor import reconcile
from lib.dependency_store import DependencyConflict, DependencyStore

NOW = datetime(2026, 11, 18, tzinfo=UTC)


@pytest.fixture
def plan():
    nodes = []
    for role in ('parent', 'child'):
        nodes.append(dict(key=role, role=role, lab_id=len(nodes)+1,
                          ci_name=f'{role}.event', catalog_namespace='babylon-catalog-event',
                          count=1, users=1, deploy_on=NOW.isoformat(),
                          auto_destroy=(NOW+timedelta(days=1)).isoformat(),
                          workshop_name=f'test-{role}', parameters={}, bindings=[]))
    nodes[1]['bindings'] = [dict(resource_name='leaderboard', source_key='password',
                                target_parameter='password', sensitive=True)]
    return DependencyPlan.model_validate(dict(run_id='run-1', event_id=1, cluster_id=1,
        namespace='test-ns', scope_key='session:1', revision=1, session_ids=[1], nodes=nodes))


@pytest.fixture
def store(tmp_path):
    with DependencyStore(tmp_path/'dependencies.sqlite3') as value:
        yield value


def start(store, plan):
    store.stage(plan, target='events')
    return store.start(plan.run_id, 'request-1')


def test_immutable_staging_idempotency_and_restart(store, plan):
    assert store.stage(plan, target='events') == plan.run_id
    assert store.stage(plan, target='events') == plan.run_id
    changed = plan.model_copy(deep=True)
    changed.revision = 2
    with pytest.raises(DependencyConflict):
        store.stage(changed, target='events')
    run = start(store, plan)
    assert store.start(plan.run_id, 'request-1') == run
    with pytest.raises(DependencyConflict):
        store.start(plan.run_id, 'different')
    path = store.path
    store.close()
    with DependencyStore(path) as restored:
        assert restored.status(run)['nodes'][0]['state'] == 'queued'


def test_leases_exclude_duplicate_worker_and_expire(store, plan):
    run = start(store, plan)
    assert store.acquire(run, 'one', now=100, seconds=10)
    assert not store.acquire(run, 'two', now=101, seconds=10)
    assert store.acquire(run, 'two', now=111, seconds=10)
    with pytest.raises(DependencyConflict):
        store.update_node(run, 'parent', 'one', state='ready')


class Adapter:
    def __init__(self):
        self.objects = {}
        self.created = []
        self.removed = []
        self.ready = False
        self.outputs = False
        self.child_absent = True

    def validate(self, node, parameters, *, pending=frozenset()):
        assert set(parameters) <= {'password'}

    def submit(self, plan, node, token, parameters):
        self.created.append((node.key, dict(parameters)))
        self.objects[node.key] = dict(workshop_uid=f'w-{node.key}', provision_uid=f'p-{node.key}')
        return self.objects[node.key]

    def observe(self, plan, node, record):
        if node.key not in self.objects:
            return {'state': 'absent'}
        return dict(state='ready' if self.ready else 'scheduled', **self.objects[node.key],
                    claim_name='claim-parent', claim_uid='rc-parent')

    def read_parent_claim(self, plan, record):
        return {'metadata': {'uid': 'rc-parent'}, 'status': {'resources': [{
            'name': 'leaderboard', 'state': {'kind': 'AnarchySubject', 'spec': {'vars': {
                'provision_data': {'password': 'synthetic-secret'} if self.outputs else {}}}}}]}}

    def destroy(self, plan, node, record):
        self.removed.append(node.key)
        if node.role != 'child' or self.child_absent:
            self.objects.pop(node.key, None)

    def absent(self, plan, node, record):
        return node.key not in self.objects


def test_parent_readiness_outputs_and_restart_gate(store, plan):
    run = start(store, plan)
    adapter = Adapter()
    for _ in range(3):
        reconcile(store, run, adapter, now=NOW)
    assert [x[0] for x in adapter.created] == ['parent']
    adapter.ready = True
    reconcile(store, run, adapter, now=NOW)
    assert [x[0] for x in adapter.created] == ['parent']
    assert store.status(run)['nodes'][0]['state'] == 'waiting_outputs'
    adapter.outputs = True
    reconcile(store, run, adapter, now=NOW)
    assert adapter.created[-1] == ('child', {'password': 'synthetic-secret'})
    reconcile(store, run, adapter, now=NOW)
    assert store.status(run)['state'] == 'ready'
    assert 'synthetic-secret' not in json.dumps(store.status(run))
    assert b'synthetic-secret' not in store.path.read_bytes()


def test_cancel_preserves_resources_and_explicit_child_first_destroy(store, plan):
    run = start(store, plan)
    adapter = Adapter()
    adapter.ready = adapter.outputs = True
    reconcile(store, run, adapter, now=NOW)
    reconcile(store, run, adapter, now=NOW)
    store.request(run, 'cancel')
    reconcile(store, run, adapter, now=NOW)
    assert adapter.removed == []
    store.request(run, 'destroy')
    adapter.child_absent = False
    reconcile(store, run, adapter, now=NOW)
    assert adapter.removed == ['child']
    assert 'parent' in adapter.objects
    adapter.child_absent = True
    reconcile(store, run, adapter, now=NOW)
    reconcile(store, run, adapter, now=NOW)
    assert adapter.removed[-1] == 'parent'
    assert store.status(run)['state'] == 'destroyed'


def test_ambiguous_submission_never_blind_retries(store, plan):
    run = start(store, plan)
    adapter = Adapter()
    def lost(*args):
        Adapter.submit(adapter, *args)
        raise TimeoutError('synthetic-secret')
    adapter.submit = lost
    reconcile(store, run, adapter, now=NOW)
    reconcile(store, run, adapter, now=NOW)
    assert len(adapter.created) == 1
    assert 'synthetic-secret' not in json.dumps(store.status(run))


def test_timeout_and_expiration_prevent_child_order(store, plan):
    run = start(store, plan)
    adapter = Adapter()
    reconcile(store, run, adapter, now=NOW)
    reconcile(store, run, adapter, now=NOW+timedelta(hours=5))
    assert store.status(run)['state'] == 'failed'
    assert [x[0] for x in adapter.created] == ['parent']


def test_single_instance_lock(store):
    with pytest.raises(DependencyConflict):
        DependencyStore(store.path)


def test_explicit_failed_child_retry_cleans_resources_and_preserves_parent(store, plan):
    run = start(store, plan)
    adapter = Adapter()
    adapter.ready = adapter.outputs = True
    reconcile(store, run, adapter, now=NOW)
    reconcile(store, run, adapter, now=NOW)
    assert store.acquire(run, 'test')
    store.update_node(run, 'child', 'test', state='failed', error_code='provision_failed')
    store.release(run, 'test')
    store.retry(run, ['child'])
    reconcile(store, run, adapter, now=NOW)
    reconcile(store, run, adapter, now=NOW)
    assert [k for k,p in adapter.created].count('parent') == 1
    assert [k for k,p in adapter.created].count('child') == 2
    assert adapter.removed == ['child']
