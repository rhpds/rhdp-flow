"""Real executor/store/Babylon adapter against synthetic controller transitions."""
# ruff: noqa: F811 -- imported pytest fixtures are resolved by argument name
import copy
import json
from datetime import timedelta
from pathlib import Path

from lib.dependency_babylon import BabylonDependencyAdapter
from lib.dependency_contract import DependencyPlan
from lib.dependency_executor import reconcile
from tests.test_dependency_runtime import NOW, Adapter, plan, start, store  # noqa: F401


class Cluster:
    def __init__(self, plan):
        self.plan = plan
        self.objects = {}
        self.creates = []
        self.deletes = []

    def command(self, args, payload=None):
        if args[0] == 'create':
            if payload['kind'] == 'SelfSubjectAccessReview':
                return {'status': {'allowed': True}}
            obj = copy.deepcopy(payload)
            meta = obj['metadata']
            meta['uid'] = f'{obj["kind"]}-{meta["name"]}'
            key = (obj['kind'].lower(), meta['name'])
            assert key not in self.objects
            self.objects[key] = obj
            self.creates.append(obj)
            return copy.deepcopy(obj)
        if args[0] == 'get':
            kind = args[1].split('.')[0]
            if kind == 'resourceproviders':
                return {'spec': {}}
            if kind == 'catalogitem':
                params = [] if args[2].startswith('parent') else [
                    {'name': 'password', 'required': True, 'openAPIV3Schema': {'type': 'string'}}]
                return {'spec': {'parameters': params}}
            if kind == 'resourceclaims':
                uid = args[args.index('-l')+1].split('=', 1)[1]
                return {'items': [copy.deepcopy(o) for (k,n),o in self.objects.items()
                    if k == 'resourceclaim' and o['metadata'].get('labels', {}).get(
                        'babylon.gpte.redhat.com/workshop-uid') == uid]}
            if kind == 'resourcehandles':
                kind = 'resourcehandle'
            return copy.deepcopy(self.objects.get((kind, args[2])))
        if args[0] == 'delete':
            parts = args[2].split('/')
            kind, name = parts[-2].rstrip('s'), parts[-1]
            obj = self.objects[(kind, name)]
            assert payload['preconditions']['uid'] == obj['metadata']['uid']
            del self.objects[(kind, name)]
            self.deletes.append((kind, name))
            return None
        raise AssertionError(args)

    def ready(self, key):
        node = next(n for n in self.plan.nodes if n.key == key)
        ws = self.objects[('workshop', node.workshop_name)]
        wp = self.objects[('workshopprovision', node.workshop_name)]
        claim_name = f'claim-{key}'
        ws['status'] = {'provisionCount': {'ordered': 1, 'active': 1},
                        'resourceClaims': {claim_name: {'uid': f'uid-{claim_name}'}}}
        wp['status'] = {'activeCount': 1}
        self.objects[('resourceclaim', claim_name)] = {'metadata': {
            'name': claim_name, 'namespace': self.plan.namespace, 'uid': f'uid-{claim_name}',
            'labels': {'babylon.gpte.redhat.com/workshop-uid': ws['metadata']['uid']}},
            'status': {'ready': True, 'healthy': True,
                'resourceHandle': {'name': f'handle-{key}', 'namespace': 'poolboy'},
                'resources': [{'name': 'leaderboard', 'state': {'kind': 'AnarchySubject',
                    'status': {'towerJobs': {'provision': {'completeTimestamp': 'done'}}},
                    'spec': {'vars': {'current_state': 'started', 'provision_data': {'password': 'synthetic-secret'}}}}}]}}
        self.objects[('resourcehandle', f'handle-{key}')] = {'metadata': {'uid': f'handle-uid-{key}'},
            'spec': {'resourceClaim': {'name': claim_name, 'namespace': self.plan.namespace}}}


def test_real_adapter_lifecycle_and_child_handle_holds_parent(plan, store, monkeypatch):
    monkeypatch.setenv('RHDP_DEPENDENCY_POOLBOY_NAMESPACE', 'poolboy')
    cluster = Cluster(plan)
    adapter = BabylonDependencyAdapter(None, command=cluster.command)
    run = start(store, plan)
    reconcile(store, run, adapter, now=NOW)
    assert len(cluster.creates) == 2
    assert all('end' not in o['spec']['lifespan'] for o in cluster.creates)
    reconcile(store, run, adapter, now=NOW)
    assert len(cluster.creates) == 2
    cluster.ready('parent')
    reconcile(store, run, adapter, now=NOW)
    assert len(cluster.creates) == 4
    assert cluster.creates[-1]['spec']['parameters']['password'] == 'synthetic-secret'
    cluster.ready('child')
    reconcile(store, run, adapter, now=NOW)
    assert store.status(run)['state'] == 'ready'
    assert 'synthetic-secret' not in json.dumps(store.status(run))
    assert b'synthetic-secret' not in store.path.read_bytes()
    store.request(run, 'destroy')
    for _ in range(4):
        reconcile(store, run, adapter, now=NOW)
    assert cluster.deletes == [('workshopprovision', 'test-child'), ('resourceclaim', 'claim-child')]
    assert ('workshop', 'test-parent') in cluster.objects
    cluster.objects.pop(('resourcehandle', 'handle-child'))
    for _ in range(4):
        reconcile(store, run, adapter, now=NOW)
    assert cluster.deletes[2] == ('workshop', 'test-child')
    assert ('workshop', 'test-parent') in cluster.objects
    cluster.objects.pop(('resourcehandle', 'handle-parent'))
    for _ in range(3):
        reconcile(store, run, adapter, now=NOW)
    assert store.status(run)['state'] == 'destroyed'


def test_shared_consumers_must_explicitly_release(plan, store):
    plan.scope = 'shared_group'
    plan.session_ids = [1, 2]
    run = start(store, plan)
    adapter = Adapter()
    adapter.ready = adapter.outputs = True
    reconcile(store, run, adapter, now=NOW)
    reconcile(store, run, adapter, now=NOW)
    store.request(run, 'destroy')
    store.release_consumer(run, 1)
    reconcile(store, run, adapter, now=NOW)
    assert 'parent' not in adapter.removed
    assert store.status(run)['state'] == 'waiting_consumers'
    store.release_consumer(run, 2)
    reconcile(store, run, adapter, now=NOW)
    assert adapter.removed[-1] == 'parent'


def test_child_schedule_and_expiration(plan, store):
    plan.nodes[1].deploy_on = NOW + timedelta(hours=1)
    run = start(store, plan)
    adapter = Adapter()
    adapter.ready = adapter.outputs = True
    reconcile(store, run, adapter, now=NOW)
    reconcile(store, run, adapter, now=NOW)
    assert len(adapter.created) == 1
    reconcile(store, run, adapter, now=NOW+timedelta(days=2))
    assert len(adapter.created) == 1
    assert store.status(run)['nodes'][1]['error_code'] == 'deployment_window_expired'


def test_cancel_racing_before_durable_submission_prevents_order(plan, store):
    run = start(store, plan)
    adapter = Adapter()
    original = adapter.validate
    def cancel_before_intent(*args, **kwargs):
        original(*args, **kwargs)
        store.request(run, 'cancel')
    adapter.validate = cancel_before_intent
    reconcile(store, run, adapter, now=NOW)
    assert adapter.created == []


def test_contract_fixture_parity(plan):
    fixture = Path(__file__).parents[1]/'contracts'/'parent_child_v1.json'
    assert DependencyPlan.model_validate_json(fixture.read_text()) == plan


def test_restart_between_workshop_and_provision_recovers_exact_owned_shell(plan, store, monkeypatch):
    monkeypatch.setenv('RHDP_DEPENDENCY_POOLBOY_NAMESPACE', 'poolboy')
    cluster = Cluster(plan)
    original = cluster.command
    def interrupted(args, payload=None):
        if args[0] == 'create' and payload['kind'] == 'WorkshopProvision':
            raise TimeoutError('synthetic-secret')
        return original(args, payload)
    adapter = BabylonDependencyAdapter(None, command=interrupted)
    run = start(store, plan)
    reconcile(store, run, adapter, now=NOW)
    assert len(cluster.creates) == 1
    adapter.command = original
    reconcile(store, run, adapter, now=NOW)
    assert len(cluster.creates) == 2
    assert store.status(run)['nodes'][0]['state'] == 'provisioning'


def test_successful_early_child_expiry_does_not_strand_later_child_or_parent(plan, store):
    plan.nodes[1].auto_destroy = NOW + timedelta(hours=1)
    later = plan.nodes[1].model_copy(deep=True)
    later.key = 'later'
    later.workshop_name = 'test-later'
    later.auto_destroy = NOW + timedelta(hours=2)
    plan.nodes.append(later)
    adapter = Adapter()
    adapter.ready = adapter.outputs = True
    run = start(store, plan)
    reconcile(store, run, adapter, now=NOW)
    reconcile(store, run, adapter, now=NOW)
    reconcile(store, run, adapter, now=NOW)
    assert store.status(run)['state'] == 'ready'
    reconcile(store, run, adapter, now=NOW+timedelta(hours=1))
    assert adapter.removed == ['child']
    assert 'later' in adapter.objects and 'parent' in adapter.objects
    assert store.status(run)['state'] == 'ready'
    for _ in range(3):
        reconcile(store, run, adapter, now=NOW+timedelta(hours=2))
    assert adapter.removed == ['child', 'later', 'parent']
    assert store.status(run)['state'] == 'destroyed'


def test_absent_unready_child_with_unrecorded_handle_cannot_release_parent(plan, store, monkeypatch):
    monkeypatch.setenv('RHDP_DEPENDENCY_POOLBOY_NAMESPACE', 'poolboy')
    cluster = Cluster(plan)
    adapter = BabylonDependencyAdapter(None, command=cluster.command)
    run = start(store, plan)
    reconcile(store, run, adapter, now=NOW)
    cluster.ready('parent')
    reconcile(store, run, adapter, now=NOW)
    # Child submission was accepted, but no ready observation ever captured its
    # claim/handle identities. A stale independent handle must hold the parent.
    cluster.objects.pop(('workshop', 'test-child'))
    cluster.objects.pop(('workshopprovision', 'test-child'))
    cluster.objects[('resourcehandle', 'unknown-child')] = {'metadata': {'uid': 'unknown'}}
    store.request(run, 'destroy')
    reconcile(store, run, adapter, now=NOW)
    assert ('workshop', 'test-parent') in cluster.objects
    assert ('resourcehandle', 'unknown-child') in cluster.objects
    assert store.status(run)['state'] == 'attention_required'
