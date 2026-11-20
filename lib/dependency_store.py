"""Transactional immutable plans and leased run state on a private persistent PVC.

SQLite execution supports one service process only. A process-held advisory lock
enforces that restriction; separate replicas/PVCs are rejected by startup config.
No output values or exception strings may be passed to this store.
"""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from lib.dependency_contract import DependencyPlan


class DependencyConflict(ValueError):
    pass


class DependencyStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._worker_locks_guard = threading.Lock()
        self._worker_locks = {}
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = open(str(path)+'.lock', 'a+')
        os.chmod(self._lock.name, 0o600)
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise DependencyConflict('dependency_single_instance_required') from None
        with self.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY, target TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY REFERENCES plans(id), idempotency_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL, action TEXT, lease_owner TEXT, lease_until REAL);
                CREATE TABLE IF NOT EXISTS nodes (
                    run_id TEXT NOT NULL REFERENCES runs(id), key TEXT NOT NULL, role TEXT NOT NULL,
                    state TEXT NOT NULL, token TEXT NOT NULL UNIQUE, attempt INTEGER NOT NULL DEFAULT 1,
                    workshop_uid TEXT, provision_uid TEXT, claim_name TEXT, claim_uid TEXT,
                    deadline REAL, error_code TEXT, claim_refs TEXT, handle_refs TEXT,
                    retry_requested INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(run_id,key));
                CREATE TABLE IF NOT EXISTS consumers (
                    run_id TEXT NOT NULL REFERENCES runs(id), session_id INTEGER NOT NULL,
                    released INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(run_id,session_id));
            ''')
        os.chmod(self.path, 0o600)

    def close(self):
        if not self._lock.closed:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def stage(self, plan: DependencyPlan, *, target: str):
        payload = json.dumps(plan.model_dump(mode='json'), sort_keys=True, separators=(',', ':'))
        with self.transaction() as db:
            old = db.execute('SELECT * FROM plans WHERE id=?', (plan.run_id,)).fetchone()
            if old and (old['payload'] != payload or old['target'] != target):
                raise DependencyConflict('immutable_dependency_plan')
            db.execute('INSERT OR IGNORE INTO plans VALUES (?,?,?)', (plan.run_id, target, payload))
        return plan.run_id

    def plan(self, plan_id):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM plans WHERE id=?', (plan_id,)).fetchone()
            if not row:
                raise KeyError('dependency_plan_not_found')
            return DependencyPlan.model_validate_json(row['payload']), row['target']

    def start(self, plan_id, idempotency_key):
        plan, _ = self.plan(plan_id)
        with self.transaction() as db:
            row = db.execute('SELECT * FROM runs WHERE id=? OR idempotency_key=?',
                             (plan_id, idempotency_key)).fetchone()
            if row:
                if row['id'] != plan_id or row['idempotency_key'] != idempotency_key:
                    raise DependencyConflict('dependency_idempotency_conflict')
                return row['id']
            db.execute('INSERT INTO runs(id,idempotency_key,state) VALUES (?,?,?)',
                       (plan_id, idempotency_key, 'active'))
            for node in plan.nodes:
                db.execute('INSERT INTO nodes(run_id,key,role,state,token) VALUES (?,?,?,?,?)',
                           (plan_id, node.key, node.role,
                            'queued' if node.role == 'parent' else 'blocked', f'{plan_id}/{node.key}/1'))
            for session_id in plan.session_ids:
                db.execute('INSERT INTO consumers(run_id,session_id) VALUES (?,?)', (plan_id, session_id))
        return plan_id

    def worker_lock(self, run_id):
        with self._worker_locks_guard:
            return self._worker_locks.setdefault(run_id, threading.Lock())

    def acquire(self, run_id, owner, *, now=None, seconds=120):
        now = time.time() if now is None else now
        with self.transaction() as db:
            cursor = db.execute('''UPDATE runs SET lease_owner=?,lease_until=? WHERE id=?
                AND (lease_until IS NULL OR lease_until<=? OR lease_owner=?)''',
                (owner, now+seconds, run_id, now, owner))
            return cursor.rowcount == 1

    def release(self, run_id, owner):
        with self.transaction() as db:
            db.execute('UPDATE runs SET lease_owner=NULL,lease_until=NULL WHERE id=? AND lease_owner=?',
                       (run_id, owner))

    @staticmethod
    def _owned(db, run_id, owner):
        row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not row or row['lease_owner'] != owner or row['lease_until'] <= time.time():
            raise DependencyConflict('dependency_lease_lost')
        return row

    def update_node(self, run_id, key, owner, **fields):
        allowed = {'state', 'workshop_uid', 'provision_uid', 'claim_name', 'claim_uid',
                   'deadline', 'error_code', 'claim_refs', 'handle_refs'}
        if not fields or fields.keys()-allowed:
            raise ValueError('invalid_dependency_state_update')
        with self.transaction() as db:
            self._owned(db, run_id, owner)
            db.execute('UPDATE nodes SET '+','.join(f'{k}=?' for k in fields)+' WHERE run_id=? AND key=?',
                       (*fields.values(), run_id, key))

    def update_run(self, run_id, owner, state):
        with self.transaction() as db:
            self._owned(db, run_id, owner)
            db.execute('UPDATE runs SET state=? WHERE id=?', (state, run_id))

    def status(self, run_id):
        with self.transaction() as db:
            run = db.execute('SELECT id,state,action FROM runs WHERE id=?', (run_id,)).fetchone()
            if not run:
                raise KeyError('dependency_run_not_found')
            nodes = db.execute('SELECT * FROM nodes WHERE run_id=? ORDER BY role DESC,key', (run_id,))
            consumers = db.execute('SELECT session_id,released FROM consumers WHERE run_id=? ORDER BY session_id', (run_id,))
            return {**dict(run), 'nodes': [dict(n) for n in nodes],
                    'consumers': [{**dict(c), 'released': bool(c['released'])} for c in consumers]}

    def request(self, run_id, action):
        if action not in {'cancel', 'destroy'}:
            raise ValueError('invalid_dependency_action')
        with self.transaction() as db:
            row = db.execute('SELECT action FROM runs WHERE id=?', (run_id,)).fetchone()
            if not row:
                raise KeyError('dependency_run_not_found')
            if row['action'] == 'destroy' and action != 'destroy':
                raise DependencyConflict('dependency_teardown_started')
            db.execute('UPDATE runs SET action=? WHERE id=?', (action, run_id))
            if action == 'destroy':
                count = db.execute('SELECT COUNT(*) FROM consumers WHERE run_id=?', (run_id,)).fetchone()[0]
                if count == 1:
                    db.execute('UPDATE consumers SET released=1 WHERE run_id=?', (run_id,))

    def release_consumer(self, run_id, session_id):
        with self.transaction() as db:
            cursor = db.execute('UPDATE consumers SET released=1 WHERE run_id=? AND session_id=?', (run_id, session_id))
            if cursor.rowcount != 1:
                raise DependencyConflict('dependency_consumer_not_found')

    def begin_submission(self, run_id, key, owner, deadline):
        with self.transaction() as db:
            run = self._owned(db, run_id, owner)
            if run['action']:
                return False
            db.execute('UPDATE nodes SET state=?,deadline=? WHERE run_id=? AND key=?',
                       ('submitting', deadline, run_id, key))
            return True

    def retry(self, run_id, keys):
        if not keys or len(set(keys)) != len(keys):
            raise DependencyConflict('explicit_failed_children_required')
        with self.transaction() as db:
            run = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if not run or run['action'] or (run['lease_until'] or 0) > time.time():
                raise DependencyConflict('dependency_run_not_retryable')
            rows = [db.execute('SELECT * FROM nodes WHERE run_id=? AND key=?',
                               (run_id, key)).fetchone() for key in keys]
            # Known submitted failures are cleaned and confirmed absent by the
            # worker first. Ambiguous submissions cannot be blindly replaced.
            if any(not n or n['role'] != 'child' or n['state'] != 'failed' or
                   (n['deadline'] and not n['workshop_uid']) for n in rows):
                raise DependencyConflict('dependency_child_not_retryable')
            for key in keys:
                db.execute('UPDATE nodes SET retry_requested=1 WHERE run_id=? AND key=?', (run_id, key))
            db.execute('UPDATE runs SET state=? WHERE id=?', ('active', run_id))

    def finish_retry(self, run_id, key, owner):
        with self.transaction() as db:
            self._owned(db, run_id, owner)
            row = db.execute('SELECT attempt FROM nodes WHERE run_id=? AND key=? AND retry_requested=1',
                             (run_id, key)).fetchone()
            if not row:
                raise DependencyConflict('dependency_retry_not_requested')
            attempt = row['attempt']+1
            db.execute('''UPDATE nodes SET state='blocked',retry_requested=0,attempt=?,token=?,
                deadline=NULL,error_code=NULL,workshop_uid=NULL,provision_uid=NULL,
                claim_uid=NULL,claim_name=NULL,claim_refs=NULL,handle_refs=NULL WHERE run_id=? AND key=?''',
                (attempt, f'{run_id}/{key}/{attempt}', run_id, key))

    def pending(self):
        with self.transaction() as db:
            return [r[0] for r in db.execute("""SELECT id FROM runs WHERE state!='destroyed'
                AND (state!='cancelled' OR action='destroy')""")]

    def owned_plans(self, target):
        with self.transaction() as db:
            rows = db.execute('''SELECT plans.payload FROM plans JOIN runs ON runs.id=plans.id
                WHERE plans.target=? AND runs.state!='destroyed' ''', (target,))
            return [DependencyPlan.model_validate_json(r[0]) for r in rows]

    def scheduled_teardown(self, run_id, now):
        plan, _ = self.plan(run_id)
        if now < max(n.auto_destroy for n in plan.nodes if n.role == 'child'):
            return
        with self.transaction() as db:
            row = db.execute('SELECT state,action FROM runs WHERE id=?', (run_id,)).fetchone()
            # Preserve evidence from every failed, incomplete or unknown attempt.
            if row and row['state'] == 'ready' and not row['action']:
                db.execute("UPDATE runs SET action='destroy' WHERE id=?", (run_id,))
                db.execute('UPDATE consumers SET released=1 WHERE run_id=?', (run_id,))
