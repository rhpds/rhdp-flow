"""One durable reconciliation step, with outputs held only in local memory.

The adapter must use bounded IO, exact deterministic object identities and
run/token ownership labels. An absent observation after a submitting intent is
ambiguous and is never automatically submitted again.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from lib.dependency_outputs import OutputBindingError, resolve_parent_outputs


def reconcile(store, run_id, adapter, *, now=None):
    lock = store.worker_lock(run_id)
    if not lock.acquire(blocking=False):
        return
    try:
        _reconcile(store, run_id, adapter, now=now)
    finally:
        lock.release()


def _reconcile(store, run_id, adapter, *, now=None):
    now = now or datetime.now(UTC)
    owner = str(uuid.uuid4())
    if not store.acquire(run_id, owner):
        return
    try:
        store.scheduled_teardown(run_id, now)
        plan, _ = store.plan(run_id)
        status = store.status(run_id)
        records = {n['key']: n for n in status['nodes']}
        parent = next(n for n in plan.nodes if n.role == 'parent')
        children = [n for n in plan.nodes if n.role == 'child']

        def update(node, **fields):
            store.update_node(run_id, node.key, owner, **fields)
            records[node.key].update(fields)

        def failed(node, code):
            update(node, state='failed', error_code=code)

        def teardown_node(node):
            record = records[node.key]
            if not record['workshop_uid']:
                observed = adapter.observe(plan, node, record)
                if observed.get('state') == 'absent':
                    failed(node, 'ambiguous_submission_requires_attention')
                    return
                _check_identity(record, observed)
                update(node, **_identities(observed))
            # A missing Workshop does not prove its asynchronous handles gone.
            # Complete durable inventory is mandatory even for absent objects.
            if hasattr(adapter, 'deletion_inventory'):
                update(node, **adapter.deletion_inventory(plan, node, record))
            update(node, state='destroying')
            if not adapter.absent(plan, node, record):
                adapter.destroy(plan, node, record)
            if adapter.absent(plan, node, record):
                update(node, state='destroyed')

        if status['action'] == 'destroy':
            # Cancellation of every unsubmitted node is durable before deletion.
            for node in plan.nodes:
                record = records[node.key]
                if (record['state'] in {'blocked', 'queued', 'cancelled', 'failed'} and
                    record['deadline'] is None and not record['workshop_uid'] and not record['provision_uid']):
                    update(node, state='destroyed')
            for group in (children, [parent]):
                if group == [parent] and any(not c['released'] for c in store.status(run_id)['consumers']):
                    store.update_run(run_id, owner, 'waiting_consumers')
                    return
                for node in group:
                    record = records[node.key]
                    if record['state'] == 'destroyed':
                        continue
                    teardown_node(node)
                if any(records[n.key]['state'] != 'destroyed' for n in group):
                    store.update_run(run_id, owner, 'destroying')
                    return
            store.update_run(run_id, owner, 'destroyed')
            return
        if status['action'] == 'cancel':
            for node in plan.nodes:
                if records[node.key]['state'] in {'queued', 'blocked'}:
                    update(node, state='cancelled')
            store.update_run(run_id, owner, 'cancelled')
            return

        if hasattr(adapter, 'parent_lifetime_capability'):
            adapter.parent_lifetime_capability(plan, parent)

        # Successful children can end before later consumers. Settle each at its
        # own end while holding the parent; incomplete/failed attempts retain
        # their evidence and never enter this automatic cleanup path.
        for node in children:
            if now >= node.auto_destroy and records[node.key]['state'] in {'ready', 'destroying'}:
                teardown_node(node)

        for node in children:
            record = records[node.key]
            if not record.get('retry_requested'):
                continue
            if record.get('workshop_uid'):
                if hasattr(adapter, 'deletion_inventory'):
                    update(node, **adapter.deletion_inventory(plan, node, record))
                adapter.destroy(plan, node, record)
                if not adapter.absent(plan, node, record):
                    continue
            store.finish_retry(run_id, node.key, owner)
            records[node.key] = next(n for n in store.status(run_id)['nodes'] if n['key'] == node.key)

        # Observe existing submissions before considering any new orders.
        for node in [parent, *children]:
            record = records[node.key]
            if record['state'] in {'queued', 'blocked', 'failed', 'cancelled', 'destroyed', 'destroying'}:
                continue
            observed = adapter.observe(plan, node, record)
            if observed.get('state') == 'absent':
                failed(node, 'resource_missing' if record['workshop_uid'] else
                       'ambiguous_submission_requires_attention')
                continue
            _check_identity(record, observed)
            state = observed.get('state')
            if state == 'incomplete' and hasattr(adapter, 'resume'):
                update(node, **_identities(observed))
                if now >= node.auto_destroy or (record['deadline'] and now.timestamp() >= record['deadline']):
                    failed(node, 'readiness_timeout')
                    continue
                values = dict(node.parameters)
                if node.role == 'child':
                    if records[parent.key]['state'] != 'ready':
                        continue
                    claim = adapter.read_parent_claim(plan, records[parent.key])
                    for child in children:
                        resolve_parent_outputs(claim, records[parent.key]['claim_uid'], child.bindings)
                    values.update(resolve_parent_outputs(claim, records[parent.key]['claim_uid'], node.bindings))
                adapter.validate(node, values)
                if store.status(run_id)['action']:
                    continue
                # Only an exact owned shell with no recorded provision UID may
                # resume. The adapter refuses to recreate any recorded UID.
                identity = adapter.resume(plan, node, record, values)
                update(node, state='provisioning', error_code=None, **_identities(identity))
                continue
            if state not in {'scheduled', 'provisioning', 'ready', 'failed'}:
                failed(node, 'resource_state_invalid')
                continue
            if state == 'ready' and (not observed.get('workshop_uid') or
                                     not observed.get('provision_uid') or
                                     (node.role == 'parent' and not observed.get('claim_uid'))):
                failed(node, 'resource_identity_missing')
                continue
            update(node, state=state, **_identities(observed))
            if state == 'failed':
                failed(node, 'provision_failed')
            elif record['deadline'] and now.timestamp() >= record['deadline'] and state != 'ready':
                failed(node, 'readiness_timeout')

        # Resolve all outputs and validate all inputs before creating child objects.
        parameters = {}
        parent_record = records[parent.key]
        if parent_record['state'] == 'ready':
            claim = adapter.read_parent_claim(plan, parent_record)
            try:
                for node in children:
                    if records[node.key]['state'] != 'blocked':
                        continue
                    values = resolve_parent_outputs(claim, parent_record['claim_uid'], node.bindings)
                    values = {**node.parameters, **values}
                    adapter.validate(node, values)
                    parameters[node.key] = values
            except OutputBindingError as exc:
                if exc.retryable and now.timestamp() < parent_record['deadline']:
                    update(parent, state='waiting_outputs')
                else:
                    failed(parent, exc.code)
                parameters.clear()

        for node in [parent, *children]:
            record = records[node.key]
            if record['state'] not in {'queued', 'blocked'} or now < node.deploy_on:
                continue
            if now >= node.auto_destroy:
                failed(node, 'deployment_window_expired')
                continue
            if node.role == 'child' and (
                records[parent.key]['state'] != 'ready' or node.key not in parameters
            ):
                continue
            values = parameters.get(node.key, dict(node.parameters))
            if node.role == 'child':
                # Re-read the exact recorded parent immediately before each IO;
                # fan-out never reuses stale credentials from a previous child.
                current_claim = adapter.read_parent_claim(plan, records[parent.key])
                values = {**node.parameters, **resolve_parent_outputs(
                    current_claim, records[parent.key]['claim_uid'], node.bindings)}
            adapter.validate(node, values)
            # Intent and deadline survive a lost create response and process restart.
            deadline = max(now.timestamp(), node.deploy_on.timestamp()) + plan.readiness_timeout_minutes*60
            if not store.begin_submission(run_id, node.key, owner, deadline):
                break
            record.update(state='submitting', deadline=deadline)
            try:
                identity = adapter.submit(plan, node, record['token'], values)
                if not identity.get('workshop_uid') or not identity.get('provision_uid'):
                    raise ValueError('missing_submission_identity')
                update(node, state='provisioning', **_identities(identity))
            except Exception:
                # Raw exceptions can contain child parameter values. Never retain/log them.
                update(node, error_code='submission_outcome_unknown')
        states = [n['state'] for n in records.values()]
        result = 'ready' if all(s in {'ready', 'destroyed'} for s in states) else (
            'partial_failure' if 'failed' in states and any(s == 'ready' for s in states)
            else 'failed' if 'failed' in states else 'active')
        store.update_run(run_id, owner, result)
    except Exception:
        # Adapter errors, including credential-bearing bodies, are never exposed.
        store.update_run(run_id, owner, 'attention_required')
    finally:
        store.release(run_id, owner)


def _identities(observed):
    return {k: observed[k] for k in ('workshop_uid', 'provision_uid', 'claim_name', 'claim_uid', 'claim_refs', 'handle_refs')
            if observed.get(k)}


def _check_identity(record, observed):
    for name in ('workshop_uid', 'provision_uid', 'claim_uid', 'claim_name'):
        if record.get(name) and observed.get(name) != record[name]:
            raise ValueError('resource_identity_mismatch')
