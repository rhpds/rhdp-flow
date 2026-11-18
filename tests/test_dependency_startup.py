"""Startup locks, configured replica limits and durable worker recovery."""
import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI

from api import dependency_routes, dependency_runtime
from lib.dependency_store import DependencyStore
from tests.test_dependency_runtime import Adapter, plan, start  # noqa: F401

# ruff: noqa: F811 -- pytest fixture imports


def test_startup_requires_explicit_single_instance(monkeypatch):
    monkeypatch.setattr(dependency_routes, 'RUNTIME_READY', True)
    monkeypatch.delenv('RHDP_DEPENDENCY_INSTANCE_COUNT', raising=False)
    with pytest.raises(RuntimeError, match='explicit_single_instance'):
        asyncio.run(dependency_runtime.start(FastAPI()))


def test_startup_worker_recovers_persisted_submission(tmp_path, monkeypatch, plan):
    monkeypatch.setenv('RHDP_DEPENDENCY_INSTANCE_COUNT', '1')
    monkeypatch.setenv('RHDP_DEPENDENCY_POOLBOY_NAMESPACE', 'poolboy')
    monkeypatch.setenv('RHDP_FLOW_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(dependency_routes, 'RUNTIME_READY', True)
    with DependencyStore(tmp_path/'dependencies.sqlite3') as store:
        run = start(store, plan)
    adapter = Adapter()
    adapter.close = lambda: None
    monkeypatch.setattr(dependency_runtime, 'adapter_for_target', lambda target: adapter)
    async def exercise():
        original = dependency_runtime._step
        event = asyncio.Event()
        loop = asyncio.get_running_loop()
        def step(store):
            original(store)
            loop.call_soon_threadsafe(event.set)
        monkeypatch.setattr(dependency_runtime, '_step', step)
        app = FastAPI()
        runtime = await dependency_runtime.start(app)
        try:
            await asyncio.wait_for(event.wait(), 2)
            assert app.state.dependency_store.status(run)['nodes'][0]['deadline']
        finally:
            await dependency_runtime.shutdown(runtime)
    asyncio.run(exercise())


def test_shared_source_hashes_match_versioned_contract():
    root = Path(__file__).parents[1]
    sources = json.loads((root/'contracts/parent_child_v1.sources.json').read_text())
    for path, source in sources.items():
        content = (root/path).read_text().replace('from lib.dependency_contract import',
                                                'from app.schemas.dependency import')
        assert hashlib.sha256(content.encode()).hexdigest() == source['sha256']
