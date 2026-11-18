"""Versioned dependency transport; ordinary CSV never receives dependency plans."""
import os
from contextlib import contextmanager

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import ValidationError

from api.auth import verify_api_key
from api.identity import require_picker_access
from lib.dependency_contract import DependencyPlan, safe_dependency_validation_errors
from lib.dependency_store import DependencyConflict

# Disabled by default. Startup enforces single-instance SQLite execution; each
# parent additionally requires a verified provider/runtime expiry hold.
RUNTIME_READY = os.environ.get('RHDP_ENABLE_DEPENDENCIES', '').lower() == 'true'
class SafeDependencyRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def safe_handler(request):
            try:
                return await handler(request)
            except HTTPException:
                raise
            except RequestValidationError:
                raise HTTPException(422, 'invalid_dependency_request') from None
            except Exception:
                # Never pass external credential/subprocess failures through the
                # application's generic logger or value-error handler.
                raise HTTPException(503, 'dependency_service_unavailable') from None
        return safe_handler


router = APIRouter(dependencies=[Depends(verify_api_key)], route_class=SafeDependencyRoute)


def _store(request):
    if not RUNTIME_READY or not getattr(request.app.state, 'dependency_store', None):
        raise HTTPException(503, 'dependency_execution_disabled')
    return request.app.state.dependency_store


@contextmanager
def _adapter(request, target):
    adapter = request.app.state.dependency_adapter(target)
    try:
        yield adapter
    finally:
        if hasattr(adapter, 'close'):
            adapter.close()


def _authorize(request, target):
    from api.routes import _requested_target
    requested = (_requested_target(request) or '').strip()
    if not requested:
        raise HTTPException(422, 'explicit_dependency_target_required')
    require_picker_access(request, requested)
    require_picker_access(request, target)
    if requested != target:
        raise HTTPException(403, 'dependency_target_mismatch')


def _load(request, plan_id):
    store = _store(request)
    try:
        plan, target = store.plan(plan_id)
    except KeyError:
        raise HTTPException(404, 'dependency_plan_not_found') from None
    _authorize(request, target)
    with _adapter(request, target) as adapter:
        if hasattr(adapter, 'authorize'):
            adapter.authorize(plan.namespace)
    return store, plan, target


def _call(action):
    try:
        return action()
    except DependencyConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except KeyError:
        raise HTTPException(404, 'dependency_run_not_found') from None


@router.get('/capabilities')
def capabilities():
    return {'deployment_contracts': ['parent_child_v1'] if RUNTIME_READY else [],
            'disabled_reasons': [] if RUNTIME_READY else ['dependency_feature_disabled']}


@router.post('/dependency-plans')
def stage_plan(request: Request, payload: dict):
    store = _store(request)
    try:
        plan = DependencyPlan.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(422, safe_dependency_validation_errors(exc)) from None
    from api.routes import _requested_target, _resolve_target, _validate_namespace
    requested = (_requested_target(request) or '').strip()
    if not requested:
        raise HTTPException(422, 'explicit_dependency_target_required')
    target = _resolve_target(request, requested)
    # An immutable explicit target is mandatory; never follow a changing default.
    if not target:
        raise HTTPException(422, 'explicit_dependency_target_required')
    _validate_namespace(plan.namespace)
    try:
        with _adapter(request, target) as adapter:
            if hasattr(adapter, 'authorize'):
                adapter.authorize(plan.namespace)
            if hasattr(adapter, 'parent_lifetime_capability'):
                adapter.parent_lifetime_capability(plan, next(n for n in plan.nodes if n.role == 'parent'))
            for node in plan.nodes:
                adapter.validate(node, node.parameters,
                                 pending={b.target_parameter for b in node.bindings})
    except HTTPException:
        raise
    except Exception as exc:
        if str(exc) == 'parent_expiry_hold_unverified':
            raise HTTPException(422, 'parent_expiry_hold_unverified') from None
        raise HTTPException(422, 'dependency_catalog_validation_failed') from None
    _call(lambda: store.stage(plan, target=target))
    return {'plan_id': plan.run_id, 'version': plan.version,
            'target_cluster': target, 'namespace': plan.namespace}


@router.get('/dependency-plans/{plan_id}')
def get_plan(request: Request, plan_id: str):
    _, plan, target = _load(request, plan_id)
    return {'plan': plan.model_dump(mode='json'), 'target_cluster': target}


@router.post('/dependency-plans/{plan_id}/runs')
def start_run(request: Request, plan_id: str,
              idempotency_key: str = Header(min_length=1, max_length=200)):
    store, plan, target = _load(request, plan_id)
    with _adapter(request, target) as adapter:
        if hasattr(adapter, 'authorize'):
            adapter.authorize(plan.namespace)
    run_id = _call(lambda: store.start(plan_id, idempotency_key))
    return {'run_id': run_id, 'state': store.status(run_id)['state']}


@router.get('/dependency-runs/{run_id}')
def run_status(request: Request, run_id: str):
    store, _, _ = _load(request, run_id)
    # Only operational metadata and safe error codes. No plan values or outputs.
    return _call(lambda: store.status(run_id))


@router.post('/dependency-runs/{run_id}/cancel')
def cancel(request: Request, run_id: str):
    store, _, _ = _load(request, run_id)
    _call(lambda: store.request(run_id, 'cancel'))
    return {'run_id': run_id, 'state': 'cancellation_requested'}


@router.post('/dependency-runs/{run_id}/destroy')
def destroy(request: Request, run_id: str):
    store, plan, target = _load(request, run_id)
    with _adapter(request, target) as adapter:
        if hasattr(adapter, 'authorize'):
            adapter.authorize(plan.namespace)
    _call(lambda: store.request(run_id, 'destroy'))
    return {'run_id': run_id, 'state': 'destruction_requested'}


@router.post('/dependency-runs/{run_id}/retry')
def retry(request: Request, run_id: str, payload: dict):
    store, _, _ = _load(request, run_id)
    keys = payload.get('node_keys')
    if set(payload) != {'node_keys'} or not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise HTTPException(422, 'explicit_failed_children_required')
    _call(lambda: store.retry(run_id, keys))
    return {'run_id': run_id, 'state': 'retry_requested'}


@router.post('/dependency-runs/{run_id}/release')
def release(request: Request, run_id: str, payload: dict):
    store, _, _ = _load(request, run_id)
    if set(payload) != {'session_id'} or type(payload['session_id']) is not int:
        raise HTTPException(422, 'explicit_dependency_consumer_required')
    _call(lambda: store.release_consumer(run_id, payload['session_id']))
    return {'run_id': run_id, 'state': 'consumer_released'}
