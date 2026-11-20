import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { DependencyReview } from '../DependencyReview';
import { api } from '../../services/api';

vi.mock('../../services/api', () => ({ getSelectedTarget: () => 'events', api: {
  dependencyCapabilities: vi.fn(), dependencyPlan: vi.fn(), dependencyStart: vi.fn(),
  dependencyStatus: vi.fn(), dependencyAction: vi.fn(),
} }));

beforeEach(() => vi.resetAllMocks());

it('blocks deploy when dependency capability is absent', async () => {
  vi.mocked(api.dependencyCapabilities).mockResolvedValue({ deployment_contracts: [], disabled_reasons: ['parent_expiry_hold_unverified'] });
  render(<DependencyReview planId="run-1" />);
  expect(await screen.findByText(/parent_expiry_hold_unverified/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Deploy dependency plan' })).not.toBeInTheDocument();
});

it('deploys the staged immutable plan and masks every parameter value', async () => {
  vi.mocked(api.dependencyCapabilities).mockResolvedValue({ deployment_contracts: ['parent_child_v1'], disabled_reasons: [] });
  vi.mocked(api.dependencyPlan).mockResolvedValue({ target_cluster: 'events', plan: {
    version: 'parent_child_v1', run_id: 'run-1', namespace: 'test-ns', revision: 1, scope: 'session', session_ids: [1],
    nodes: [{ key: 'parent', role: 'parent', ci_name: 'leaderboard.event', count: 1, users: 1,
      deploy_on: '2026-11-18T00:00:00Z', auto_destroy: '2026-11-19T00:00:00Z', parameters: {}, bindings: [] },
    { key: 'child', role: 'child', ci_name: 'student.event', count: 2, users: 2,
      deploy_on: '2026-11-18T00:00:00Z', auto_destroy: '2026-11-19T00:00:00Z',
      parameters: { demo: 'synthetic-secret' }, bindings: [{ resource_name: 'leaderboard', source_key: 'password', target_parameter: 'password', sensitive: true }] }],
  } });
  vi.mocked(api.dependencyStart).mockResolvedValue({ run_id: 'run-1', state: 'active' });
  vi.mocked(api.dependencyStatus).mockResolvedValue({ id: 'run-1', state: 'active', nodes: [{ key: 'child', role: 'child', state: 'blocked' }] });
  render(<DependencyReview planId="run-1" />);
  expect(await screen.findByText('student.event')).toBeInTheDocument();
  expect(screen.getByText(/password: From parent/)).toBeInTheDocument();
  expect(screen.queryByText('synthetic-secret')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Deploy dependency plan' }));
  await waitFor(() => expect(api.dependencyStart).toHaveBeenCalledWith('run-1', 'run-1'));
  expect(await screen.findByText(/Waiting for parent/)).toBeInTheDocument();
});
