"""Single-instance dependency worker and startup recovery from persistent SQLite."""
import asyncio
import os

from api import dependency_routes
from lib.dependency_babylon import BabylonDependencyAdapter
from lib.dependency_executor import reconcile
from lib.dependency_store import DependencyStore
from lib.flow_state import data_dir


def adapter_for_target(target):
    from api.cluster_targets import cleanup_kubeconfig
    from api.routes import _get_config
    config = _get_config(target_cluster=target)
    return BabylonDependencyAdapter(config, cleanup=lambda: cleanup_kubeconfig(config.kubeconfig_path))


def _step(store):
    for run_id in store.pending():
        adapter = None
        try:
            _, target = store.plan(run_id)
            adapter = adapter_for_target(target)
            reconcile(store, run_id, adapter)
        except Exception:
            # One target's credential failure must not starve other runs.
            owner = 'credential-recovery'
            if store.acquire(run_id, owner):
                try:
                    store.update_run(run_id, owner, 'attention_required')
                finally:
                    store.release(run_id, owner)
        finally:
            if adapter:
                adapter.close()


async def start(app):
    if not dependency_routes.RUNTIME_READY:
        return None
    if (os.environ.get('RHDP_DEPENDENCY_INSTANCE_COUNT') != '1' or
        os.environ.get('WEB_CONCURRENCY', '1') != '1'):
        raise RuntimeError('dependency_sqlite_requires_explicit_single_instance')
    if not os.environ.get('RHDP_DEPENDENCY_POOLBOY_NAMESPACE'):
        raise RuntimeError('parent_expiry_hold_unverified')
    store = DependencyStore(data_dir()/'dependencies.sqlite3')
    app.state.dependency_store = store
    app.state.dependency_adapter = adapter_for_target
    stop = asyncio.Event()

    async def worker():
        while not stop.is_set():
            # Sequential steps recover all persisted intents after restart.
            # Credentials are resolved afresh for the stored immutable target.
            try:
                await asyncio.to_thread(_step, store)
            except Exception:
                # No raw subprocess/adapter exception data goes to logs.
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                pass

    return store, stop, asyncio.create_task(worker())


async def shutdown(runtime):
    if runtime:
        store, stop, task = runtime
        stop.set()
        await task
        store.close()
