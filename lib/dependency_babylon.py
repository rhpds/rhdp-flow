"""Exact-identity Babylon transport. Sensitive manifests stay on stdin/in memory."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess

from lib.dependency_lifetime import effective_deadline_verified, validate_policy
from lib.dependency_parameters import ParameterValidationError, validate_parameters


class BabylonDependencyAdapter:
    def __init__(self, config, *, command=None, cleanup=None):
        if config is None:
            from rhdp_flow import RHDPConfig
            config = RHDPConfig()
        self.config = config
        self.command = command or self._command
        self.cleanup = cleanup

    def close(self):
        if self.cleanup:
            self.cleanup()

    def _command(self, args, payload=None):
        argv = [self.config.oc_command]
        if self.config.kubeconfig_path:
            argv += ['--kubeconfig', self.config.kubeconfig_path]
        result = subprocess.run(argv+args, input=json.dumps(payload) if payload is not None else None,
                                capture_output=True, text=True, timeout=15)
        if result.returncode:
            # Never interpolate subprocess stdout/stderr, which may echo manifests.
            raise ValueError('dependency_cluster_operation_failed')
        return json.loads(result.stdout) if result.stdout.strip() else None

    def authorize(self, namespace):
        # API key + picker authorization is checked by the routes; the target's
        # ServiceAccount must also have each actual namespace permission.
        for verb in ('get', 'create', 'delete'):
            for resource in ('workshops', 'workshopprovisions', 'resourceclaims'):
                group = 'poolboy.gpte.redhat.com' if resource == 'resourceclaims' else 'babylon.gpte.redhat.com'
                result = self.command(['create', '-f', '-', '-o', 'json'], {
                    'apiVersion': 'authorization.k8s.io/v1', 'kind': 'SelfSubjectAccessReview',
                    'spec': {'resourceAttributes': {'verb': verb, 'resource': resource,
                                                    'group': group, 'namespace': namespace}}})
                if not result or result.get('status', {}).get('allowed') is not True:
                    raise ValueError('dependency_namespace_forbidden')

    def _get(self, kind, name, namespace):
        return self.command(['get', kind, name, '-n', namespace, '--ignore-not-found', '-o', 'json'])

    def validate(self, node, parameters, *, pending=frozenset()):
        catalog = self._get('catalogitem', node.ci_name, node.catalog_namespace)
        if not catalog:
            raise ParameterValidationError('catalog_item_unavailable')
        from rhdp_flow import _catalog_item_parameter_defs_by_name
        definitions = list(_catalog_item_parameter_defs_by_name(catalog.get('spec', {})).values())
        for item in definitions:
            name = item.get('name')
            spec = item.get('openAPIV3Schema') or {}
            if name in node.parameters and (item.get('sensitive') or spec.get('writeOnly') or
                                            spec.get('format') == 'password'):
                raise ParameterValidationError('static_sensitive_parameter_forbidden')
        validate_parameters(definitions, parameters, pending=set(pending))

    def parent_lifetime_capability(self, plan, parent):
        """Check catalog resource providers against the requested parent deadline."""
        namespace = os.environ.get('RHDP_DEPENDENCY_POOLBOY_NAMESPACE', '')
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', namespace):
            raise ValueError('parent_expiry_hold_unverified')
        seen = set()

        def visit(name, active):
            if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', name):
                raise ValueError('parent_expiry_hold_unverified')
            if name in active or len(seen) >= 8 or len(active) >= 8:
                raise ValueError('parent_expiry_hold_unverified')
            if name in seen:
                return
            provider = self._get('resourceproviders.poolboy.gpte.redhat.com', name, namespace)
            if not provider or provider.get('metadata', {}).get('deletionTimestamp'):
                raise ValueError('parent_expiry_hold_unverified')
            spec = provider.get('spec') or {}
            validate_policy(spec, parent.deploy_on.isoformat(), parent.auto_destroy.isoformat())
            for linked in spec.get('linkedResourceProviders') or []:
                if not isinstance(linked, dict):
                    raise ValueError('parent_expiry_hold_unverified')  # noqa: TRY004 -- safe public error taxonomy
                visit(linked.get('name'), active | {name})
            seen.add(name)

        catalog = self._get('catalogitem', parent.ci_name, parent.catalog_namespace)
        if not catalog or catalog.get('metadata', {}).get('deletionTimestamp'):
            raise ValueError('catalog_item_unavailable')
        validate_policy(catalog.get('spec') or {}, parent.deploy_on.isoformat(), parent.auto_destroy.isoformat())
        resources = catalog.get('spec', {}).get('resources')
        if not isinstance(resources, list) or not resources:
            raise ValueError('parent_expiry_hold_unverified')
        for resource in resources:
            provider = resource.get('provider') or {}
            if provider.get('namespace', namespace) != namespace:
                raise ValueError('parent_expiry_hold_unverified')
            visit(provider.get('name'), set())

    def _parent_claim_hold(self, claim, parent):
        metadata, spec, status = (claim.get(k) or {} for k in ('metadata', 'spec', 'status'))
        if (metadata.get('deletionTimestamp') or status.get('detached') or spec.get('autoDetach') or
            spec.get('autoDelete')):
            raise ValueError('parent_expiry_hold_unverified')
        ref = status.get('resourceHandle')
        if (not isinstance(ref, dict) or not ref.get('name') or ref.get('detached') or
            ref.get('namespace') != os.environ.get('RHDP_DEPENDENCY_POOLBOY_NAMESPACE')):
            raise ValueError('parent_expiry_hold_unverified')
        handle = self._get('resourcehandles.poolboy.gpte.redhat.com', ref['name'], ref['namespace'])
        if not handle or handle.get('metadata', {}).get('deletionTimestamp'):
            raise ValueError('parent_expiry_hold_unverified')
        if ref.get('uid') and handle.get('metadata', {}).get('uid') != ref['uid']:
            raise ValueError('parent_expiry_hold_unverified')
        reverse = handle.get('spec', {}).get('resourceClaim') or {}
        if (reverse.get('name') != metadata.get('name') or
            reverse.get('namespace') != metadata.get('namespace')):
            raise ValueError('parent_expiry_hold_unverified')
        return effective_deadline_verified(claim, handle, parent.auto_destroy.isoformat())

    @staticmethod
    def labels(plan, node, token):
        return {'flow.demo.redhat.com/dependency-run': hashlib.sha256(plan.run_id.encode()).hexdigest()[:32],
                'flow.demo.redhat.com/dependency-node': hashlib.sha256(node.key.encode()).hexdigest()[:32],
                'flow.demo.redhat.com/dependency-token': hashlib.sha256(token.encode()).hexdigest()[:32]}

    @staticmethod
    def _owned(obj, labels, uid=None):
        metadata = obj.get('metadata') or {}
        if (not metadata.get('uid') or metadata.get('deletionTimestamp') or
            any(metadata.get('labels', {}).get(k) != v for k, v in labels.items()) or
            (uid and metadata['uid'] != uid)):
            raise ValueError('dependency_resource_ownership_conflict')
        return metadata['uid']

    def submit(self, plan, node, token, parameters, *, expected_record=None):
        from rhdp_flow import build_workshop_provision_dict, build_workshop_resource_dict
        payload = {'metadata': {'annotations': {}}, 'spec': {
            'provider': {'name': node.ci_name, 'namespace': node.catalog_namespace,
                         'parameterValues': {}},
            'lifespan': {'end': node.auto_destroy.isoformat()}}}
        labels = self.labels(plan, node, token)
        workshop = build_workshop_resource_dict(node.workshop_name, plan.namespace, payload,
                                                self.config, False, node.catalog_namespace)
        workshop['spec']['openRegistration'] = False
        provision = build_workshop_provision_dict(node.workshop_name, plan.namespace, payload,
            self.config, 1, node.count, parameters, fetch_catalog_defaults=False)
        for obj in (workshop, provision):
            obj['metadata'].setdefault('labels', {}).update(labels)
            obj['spec']['lifespan'] = {'start': node.deploy_on.isoformat(), 'end': node.auto_destroy.isoformat()}
            obj['spec']['actionSchedule'] = {'start': node.deploy_on.isoformat()}
            if node.role == 'parent':
                obj['spec']['autoDetach'] = {}
        result = {}
        for kind, obj, field in [('workshop', workshop, 'workshop_uid'),
                                 ('workshopprovision', provision, 'provision_uid')]:
            existing = self._get(kind, node.workshop_name, plan.namespace)
            expected_uid = (expected_record or {}).get(field)
            if existing:
                result[field] = self._owned(existing, labels, expected_uid)
            else:
                if expected_uid:
                    raise ValueError('dependency_resource_deleted')
                if kind == 'workshopprovision':
                    obj['metadata']['ownerReferences'] = [{
                        'apiVersion': 'babylon.gpte.redhat.com/v1', 'kind': 'Workshop',
                        'name': node.workshop_name, 'uid': result['workshop_uid'], 'controller': True}]
                created = self.command(['create', '-f', '-', '-o', 'json'], obj)
                result[field] = self._owned(created, labels)
        return result

    def resume(self, plan, node, record, parameters):
        return self.submit(plan, node, record['token'], parameters, expected_record=record)

    def observe(self, plan, node, record):
        labels = self.labels(plan, node, record['token'])
        workshop = self._get('workshop', node.workshop_name, plan.namespace)
        provision = self._get('workshopprovision', node.workshop_name, plan.namespace)
        if not workshop and not provision:
            return {'state': 'absent'}
        if not workshop:
            raise ValueError('dependency_workshop_deleted')
        if not provision:
            if record.get('provision_uid'):
                raise ValueError('dependency_provision_deleted')
            return {'state': 'incomplete',
                    'workshop_uid': self._owned(workshop, labels, record.get('workshop_uid'))}
        result = {'workshop_uid': self._owned(workshop, labels, record.get('workshop_uid')),
                  'provision_uid': self._owned(provision, labels, record.get('provision_uid'))}
        status = provision.get('status') or {}
        result['state'] = 'provisioning'
        if status.get('failedCount', 0):
            result['state'] = 'failed'
            return result
        raw_refs = workshop.get('status', {}).get('resourceClaims') or {}
        if not isinstance(raw_refs, dict):
            raise ValueError('dependency_claim_reference_invalid')  # noqa: TRY004 -- safe public error taxonomy
        refs = [{'name': name, **reference} for name, reference in raw_refs.items()]
        # Refuse undocumented/ambiguous shapes. Never choose a latest catalog claim.
        if not isinstance(refs, list) or len(refs) != node.count:
            return result
        claims = []
        for ref in refs:
            if not isinstance(ref, dict) or not ref.get('name') or not ref.get('uid'):
                raise ValueError('dependency_claim_reference_invalid')
            claim = self._get('resourceclaim', ref['name'], plan.namespace)
            if (not claim or claim.get('metadata', {}).get('uid') != ref['uid'] or
                claim.get('metadata', {}).get('labels', {}).get('babylon.gpte.redhat.com/workshop-uid')
                != result['workshop_uid']):
                raise ValueError('parent_identity_mismatch')
            claims.append(claim)
        result['claim_refs'] = json.dumps([{'name': r['name'], 'uid': r['uid']} for r in refs], sort_keys=True)
        if node.role == 'parent':
            result.update(claim_name=refs[0]['name'], claim_uid=refs[0]['uid'])
            if not self._parent_claim_hold(claims[0], node):
                return result
        # Save provenance during provisioning as soon as each handle binds;
        # readiness must not be the first chance to retain these identities.
        result.update(self.deletion_inventory(plan, node, {**record, **result}, require_complete=False))
        def ready(claim):
            metadata, state = claim.get('metadata', {}), claim.get('status', {})
            return (not metadata.get('deletionTimestamp') and state.get('ready') is True and
                    state.get('healthy') is True and not state.get('detached'))
        counts = workshop.get('status', {}).get('provisionCount') or {}
        if counts.get('failed', 0):
            result['state'] = 'failed'
            return result
        if (counts.get('ordered') == node.count and counts.get('active') == node.count
            and all(ready(c) for c in claims)):
            if node.role == 'parent':
                for resource in claims[0].get('status', {}).get('resources') or []:
                    state = resource.get('state') or {}
                    if state.get('kind') == 'AnarchySubject':
                        current = state.get('spec', {}).get('vars', {}).get('current_state', '')
                        if (current.endswith('-failed') or current in {'provision-error', 'provision-cancelled',
                            'destroying', 'destroyed'} or state.get('metadata', {}).get('deletionTimestamp')):
                            result['state'] = 'failed'
                            return result
                        if current not in {'started', 'provisioned'}:
                            return result
                        if not state.get('status', {}).get('towerJobs', {}).get('provision', {}).get('completeTimestamp'):
                            return result
                if not claims[0].get('status', {}).get('resources'):
                    return result
            result['state'] = 'ready'
            # Capture handles while claims are alive, before a child's ordinary
            # automatic expiry can remove the only provenance reference.
            result.update(self.deletion_inventory(plan, node, {**record, **result}))
        return result

    def read_parent_claim(self, plan, record):
        parent = next(n for n in plan.nodes if n.role == 'parent')
        observed = self.observe(plan, parent, record)
        if (observed.get('state') != 'ready' or observed.get('claim_name') != record['claim_name'] or
            observed.get('claim_uid') != record['claim_uid']):
            raise ValueError('parent_claim_not_ready')
        claim = self._get('resourceclaim', record['claim_name'], plan.namespace)
        if not claim or claim.get('metadata', {}).get('uid') != record['claim_uid']:
            raise ValueError('parent_identity_mismatch')
        if not self._parent_claim_hold(claim, parent):
            raise ValueError('parent_lifetime_acknowledgement_pending')
        return claim

    def _delete(self, kind, name, namespace, uid):
        group = 'poolboy.gpte.redhat.com' if kind == 'resourceclaims' else 'babylon.gpte.redhat.com'
        path = f'/apis/{group}/v1/namespaces/{namespace}/{kind}/{name}'
        self.command(['delete', '--raw', path, '-f', '-'], {
            'apiVersion': 'v1', 'kind': 'DeleteOptions',
            'preconditions': {'uid': uid}, 'propagationPolicy': 'Foreground'})

    def deletion_inventory(self, plan, node, record, *, require_complete=True):
        claims = {r['uid']: r for r in json.loads(record.get('claim_refs') or '[]')}
        handles = {r['uid']: r for r in json.loads(record.get('handle_refs') or '[]')}
        for claim in self._claims(plan, record):
            meta = claim['metadata']
            claims[meta['uid']] = {'name': meta['name'], 'uid': meta['uid']}
            reference = claim.get('status', {}).get('resourceHandle') or {}
            if not reference.get('name') or not reference.get('namespace'):
                if require_complete:
                    raise ValueError('dependency_handle_identity_unconfirmed')
                continue
            handle = self._get('resourcehandles.poolboy.gpte.redhat.com',
                               reference['name'], reference['namespace'])
            if not handle:
                continue
            metadata = handle.get('metadata') or {}
            reverse = handle.get('spec', {}).get('resourceClaim') or {}
            if (not metadata.get('uid') or reverse.get('name') != meta['name'] or
                reverse.get('namespace') != plan.namespace):
                raise ValueError('dependency_handle_identity_conflict')
            handles[metadata['uid']] = {'name': reference['name'], 'namespace': reference['namespace'],
                                        'uid': metadata['uid']}
        if require_complete and (len(claims) != node.count or len(handles) != node.count):
            # Partial controller submissions may still acquire handles. Preserve
            # all objects until the complete ownership inventory is confirmed.
            raise ValueError('dependency_teardown_identity_inventory_incomplete')
        return {'claim_refs': json.dumps(list(claims.values()), sort_keys=True),
                'handle_refs': json.dumps(list(handles.values()), sort_keys=True)}

    def destroy(self, plan, node, record):
        labels = self.labels(plan, node, record['token'])
        # Stop provisioning first, then claims. Retain Workshop until all its
        # durable claim references have disappeared so a restart cannot lose them.
        provision = self._get('workshopprovision', node.workshop_name, plan.namespace)
        if provision:
            uid = self._owned(provision, labels, record['provision_uid'])
            self._delete('workshopprovisions', node.workshop_name, plan.namespace, uid)
            return
        for reference in json.loads(record.get('claim_refs') or '[]'):
            claim = self._get('resourceclaim', reference['name'], plan.namespace)
            if claim:
                meta = claim.get('metadata') or {}
                if meta.get('uid') != reference['uid']:
                    raise ValueError('dependency_claim_identity_conflict')
                self._delete('resourceclaims', reference['name'], plan.namespace, reference['uid'])
                return
        if not self._handles_absent(record):
            return
        workshop = self._get('workshop', node.workshop_name, plan.namespace)
        if workshop:
            uid = self._owned(workshop, labels, record['workshop_uid'])
            self._delete('workshops', node.workshop_name, plan.namespace, uid)

    def absent(self, plan, node, record):
        if self._get('workshop', node.workshop_name, plan.namespace):
            return False
        if self._get('workshopprovision', node.workshop_name, plan.namespace):
            return False
        return not self._claims(plan, record) and self._handles_absent(record)

    def _handles_absent(self, record):
        for reference in json.loads(record.get('handle_refs') or '[]'):
            handle = self._get('resourcehandles.poolboy.gpte.redhat.com', reference['name'], reference['namespace'])
            if handle:
                if handle.get('metadata', {}).get('uid') != reference['uid']:
                    raise ValueError('dependency_handle_identity_conflict')
                return False
        return True

    def _claims(self, plan, record):
        if not record.get('workshop_uid'):
            raise ValueError('dependency_workshop_identity_missing')
        label = 'babylon.gpte.redhat.com/workshop-uid'
        result = self.command(['get', 'resourceclaims', '-n', plan.namespace, '-l',
                               f'{label}={record["workshop_uid"]}', '-o', 'json'])
        if not isinstance(result, dict) or result.get('metadata', {}).get('continue'):
            raise ValueError('dependency_claim_list_incomplete')
        claims = result.get('items', [])
        if any(c.get('metadata', {}).get('labels', {}).get(label) != record['workshop_uid'] or
               not c.get('metadata', {}).get('uid') for c in claims):
            raise ValueError('dependency_claim_identity_conflict')
        return claims
