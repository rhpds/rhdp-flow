"""Production adapter object identity and parameter forwarding tests."""
# ruff: noqa: F811 -- imported pytest fixtures are resolved by argument name
import copy

import pytest

from lib.dependency_babylon import BabylonDependencyAdapter
from tests.test_dependency_runtime import plan  # noqa: F401


def test_exact_schema_and_parameter_forwarding(plan):
    calls = []
    def command(args, payload=None):
        calls.append((args, copy.deepcopy(payload)))
        if args[:2] == ['get', 'catalogitem']:
            return {'spec': {'parameters': [{'name': 'password', 'required': True,
                'openAPIV3Schema': {'type': 'string', 'minLength': 1, 'format': 'password'}}]}}
        if args[:2] == ['get', 'workshop'] or args[:2] == ['get', 'workshopprovision']:
            return None
        if args[0] == 'create':
            return {**payload, 'metadata': {**payload['metadata'], 'uid': payload['kind']+'-uid'}}
        raise AssertionError(args)
    adapter = BabylonDependencyAdapter(None, command=command)
    child = plan.nodes[1]
    adapter.validate(child, {'password': 'synthetic-secret'})
    identity = adapter.submit(plan, child, 'stable-token', {'password': 'synthetic-secret'})
    assert identity['workshop_uid'] == 'Workshop-uid'
    manifests = [p for args, p in calls if args[0] == 'create']
    assert len(manifests) == 2
    assert manifests[1]['spec']['parameters']['password'] == 'synthetic-secret'
    assert manifests[0]['metadata']['name'] == child.workshop_name
    assert manifests[1]['metadata']['labels']['flow.demo.redhat.com/dependency-token']


def test_ownership_conflict_and_partial_submission_fail_closed(plan):
    def command(args, payload=None):
        return {'metadata': {'uid': 'foreign-uid', 'labels': {}}}
    adapter = BabylonDependencyAdapter(None, command=command)
    with pytest.raises(ValueError, match='dependency_resource_ownership_conflict'):
        adapter.submit(plan, plan.nodes[0], 'token', {})


def test_parent_claim_uid_is_verified_and_failed_claim_blocks_readiness(plan, monkeypatch):
    monkeypatch.setenv('RHDP_DEPENDENCY_POOLBOY_NAMESPACE', 'poolboy')
    labels = BabylonDependencyAdapter.labels(plan, plan.nodes[0], 'token')
    objects = {
        'workshop': {'metadata': {'uid': 'w', 'labels': labels}, 'status': {
            'resourceClaims': {'claim': {'uid': 'recorded-uid'}},
            'provisionCount': {'ordered': 1, 'active': 1}}},
        'workshopprovision': {'metadata': {'uid': 'p', 'labels': labels},
            'status': {'activeCount': 1, 'failedCount': 0, 'provisioningCount': 0}},
        'resourceclaim': {'metadata': {'uid': 'replacement', 'name': 'claim', 'namespace': 'test-ns',
            'labels': {'babylon.gpte.redhat.com/workshop-uid': 'w'}},
            'status': {'ready': True, 'healthy': True, 'resourceHandle': {'name': 'handle', 'namespace': 'poolboy'}}},
        'resourcehandles.poolboy.gpte.redhat.com': {'metadata': {'uid': 'handle-uid'},
            'spec': {'resourceClaim': {'name': 'claim', 'namespace': 'test-ns'}}},
        'resourceclaims': {'items': []},
    }
    adapter = BabylonDependencyAdapter(None, command=lambda args, payload=None: objects[args[1]])
    with pytest.raises(ValueError, match='parent_identity_mismatch'):
        adapter.observe(plan, plan.nodes[0], {'token': 'token'})
    objects['resourceclaim']['metadata']['uid'] = 'recorded-uid'
    objects['resourceclaim']['status']['healthy'] = False
    assert adapter.observe(plan, plan.nodes[0], {'token': 'token'})['state'] != 'ready'


def test_parent_lifetime_hold_rejects_provider_defaults_and_recurses(plan, monkeypatch):
    monkeypatch.setenv('RHDP_DEPENDENCY_POOLBOY_NAMESPACE', 'poolboy')
    providers = {'parent.event': {'spec': {'linkedResourceProviders': [{'name': 'underlying'}]}},
                 'underlying': {'spec': {'lifespan': {'default': '48h'}}}}
    calls = []
    def command(args, payload=None):
        calls.append(args)
        return providers[args[2]]
    adapter = BabylonDependencyAdapter(None, command=command)
    with pytest.raises(ValueError, match='parent_expiry_hold_unverified'):
        adapter.parent_lifetime_capability(plan, plan.nodes[0])
    providers['underlying']['spec'] = {}
    adapter.parent_lifetime_capability(plan, plan.nodes[0])
    assert all(args[4] == 'poolboy' for args in calls)


def test_parent_lifetime_requires_configured_provider_namespace(plan, monkeypatch):
    monkeypatch.delenv('RHDP_DEPENDENCY_POOLBOY_NAMESPACE', raising=False)
    adapter = BabylonDependencyAdapter(None, command=lambda *a: pytest.fail('no guessed lookup'))
    with pytest.raises(ValueError, match='parent_expiry_hold_unverified'):
        adapter.parent_lifetime_capability(plan, plan.nodes[0])
