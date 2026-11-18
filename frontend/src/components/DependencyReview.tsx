import { useEffect, useState } from 'react';
import { api, type DependencyPlan, type DependencyStatus, getSelectedTarget } from '../services/api';

/** Review retains only a staged plan identity through Deploy; it never exports CSV. */
export function DependencyReview({ planId }: { planId: string }) {
  const [plan, setPlan] = useState<DependencyPlan>();
  const [target, setTarget] = useState('');
  const [error, setError] = useState('');
  const [status, setStatus] = useState<DependencyStatus>();
  const [busy, setBusy] = useState(false);
  const [targetVersion, setTargetVersion] = useState(0);
  useEffect(() => {
    const changed = () => { setPlan(undefined); setStatus(undefined); setTargetVersion(v => v + 1); };
    window.addEventListener('rhdp-target-change', changed);
    return () => window.removeEventListener('rhdp-target-change', changed);
  }, []);
  useEffect(() => {
    let cancelled = false;
    setError(''); setPlan(undefined); setStatus(undefined);
    (async () => {
      try {
        const capabilities = await api.dependencyCapabilities();
        if (!capabilities.deployment_contracts.includes('parent_child_v1')) {
          throw new Error(`Dependency deployment unavailable: ${capabilities.disabled_reasons?.join(', ') || 'unsupported contract'}`);
        }
        const staged = await api.dependencyPlan(planId);
        if (staged.plan.version !== 'parent_child_v1' || staged.plan.run_id !== planId) throw new Error('Invalid staged plan');
        if (!cancelled) { setPlan(staged.plan); setTarget(staged.target_cluster); }
      } catch (e) { if (!cancelled) setError(e instanceof Error ? e.message : 'Dependency review unavailable'); }
    })();
    return () => { cancelled = true; };
  }, [planId, targetVersion]);
  async function action(kind: 'deploy' | 'refresh' | 'cancel' | 'destroy' | 'retry', node?: string) {
    if (!plan || (getSelectedTarget() && getSelectedTarget() !== target)) return;
    setBusy(true); setError('');
    try {
      if (kind === 'deploy') await api.dependencyStart(planId, planId);
      else if (kind !== 'refresh') await api.dependencyAction(planId, kind, node ? [node] : undefined);
      setStatus(await api.dependencyStatus(planId));
    } catch (e) { setError(e instanceof Error ? e.message : 'Dependency action failed'); }
    finally { setBusy(false); }
  }
  const labels: Record<string, string> = { blocked: 'Waiting for parent', waiting_outputs: 'Waiting for outputs', queued: 'Ready to order' };
  return <section aria-label="Dependency plan review" style={{ padding: 24 }}>
    <h2>Parent–Child deployment review</h2>
    {error && <p role="alert">{error}</p>}
    {plan && <>
      <p>Target: {target} · Namespace: {plan.namespace} · Revision: {plan.revision} · Scope: {plan.scope}</p>
      <p>Sessions: {plan.session_ids.join(', ')}</p>
      <p>Parents supply infrastructure. Attendee environments: {plan.nodes.filter(n => n.role === 'child').reduce((a, n) => a + n.users, 0)}</p>
      <ul>{plan.nodes.map(node => <li key={node.key}>
        <strong>{node.ci_name}</strong> ({node.role}, {node.count} environments)
        <p>Start: {node.deploy_on} · Destroy: {node.auto_destroy}</p>
        <ul>{node.bindings.map(binding => <li key={binding.target_parameter}>
          {binding.target_parameter}: From parent ({binding.resource_name}.{binding.source_key})
        </li>)}{Object.keys(node.parameters).map(key => <li key={key}>{key}: configured value (masked)</li>)}</ul>
      </li>)}</ul>
      {!status && <button disabled={busy} onClick={() => action('deploy')}>Deploy dependency plan</button>}
      <button disabled={busy} onClick={() => action('refresh')}>Refresh dependency status</button>
      {status && <>
        <p role="status">Run: {status.state}</p>
        <ul>{status.nodes.map(node => <li key={node.key}>{node.key}: {labels[node.state] || node.state}
          {node.error_code && ` (${node.error_code})`}
          {node.role === 'child' && node.state === 'failed' && <button disabled={busy} onClick={() => action('retry', node.key)}>Retry {node.key}</button>}
        </li>)}</ul>
        <button disabled={busy} onClick={() => action('cancel')}>Cancel future submissions</button>
        {plan.scope === 'shared_group' && status.consumers?.filter(c => !c.released).map(c =>
          <button key={c.session_id} disabled={busy} onClick={async () => {
            setBusy(true);
            try { await api.dependencyRelease(planId, c.session_id); setStatus(await api.dependencyStatus(planId)); }
            catch { setError('Consumer release failed'); }
            finally { setBusy(false); }
          }}>Release session {c.session_id}</button>)}
        <button disabled={busy} onClick={() => { if (window.confirm('Destroy child environments, then their parent?')) void action('destroy'); }}>Destroy dependency run</button>
      </>}
      <p>Start, stop, extend and parent replacement require dependency lifecycle support and are unavailable here.</p>
    </>}
  </section>;
}
