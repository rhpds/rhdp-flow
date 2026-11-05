import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, vi } from 'vitest';
import { UploadTab } from '../UploadTab';
import { api, selectTargetCluster } from '../../services/api';
import { mockSchedule } from '../../test/mocks/api';

const noop = () => {};

// Keep the deploy-target picker out of the way by default: most tests run as an
// unauthenticated (non-allowlisted) user, so the picker is hidden. Individual
// tests override api.getClusters as needed.
afterEach(() => {
  selectTargetCluster('');
  vi.restoreAllMocks();
});

describe('UploadTab', () => {
  it('renders empty state when no schedules loaded', () => {
    render(
      <UploadTab
        dryRun={true}
        schedules={[]}
        setSchedules={noop}
        results={[]}
        setResults={noop}
        showToast={noop}
        onClear={noop}
      />
    );
    expect(screen.getByText('No schedules loaded')).toBeInTheDocument();
  });

  it('renders schedule preview table when schedules exist', () => {
    render(
      <UploadTab
        dryRun={true}
        schedules={[mockSchedule]}
        setSchedules={noop}
        results={[]}
        setResults={noop}
        showToast={noop}
        onClear={noop}
      />
    );
    expect(screen.getByText(/Schedule Preview/)).toBeInTheDocument();
    expect(screen.getByDisplayValue('Test Workshop')).toBeInTheDocument();
    expect(screen.getByDisplayValue('vendor.test.prod')).toBeInTheDocument();
  });

  it('renders upload and clear buttons', () => {
    render(
      <UploadTab
        dryRun={true}
        schedules={[]}
        setSchedules={noop}
        results={[]}
        setResults={noop}
        showToast={noop}
        onClear={noop}
      />
    );
    expect(screen.getByText('Upload')).toBeInTheDocument();
    expect(screen.getByText('Clear / New Upload')).toBeInTheDocument();
  });

  it('renders deploy settings card with schedules loaded', () => {
    render(
      <UploadTab
        dryRun={true}
        schedules={[mockSchedule]}
        setSchedules={noop}
        results={[]}
        setResults={noop}
        showToast={noop}
        onClear={noop}
      />
    );
    expect(screen.getByText('Deploy Settings')).toBeInTheDocument();
    expect(screen.getByText('Lock UI Admin Settings')).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: 'Lock row edits' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Full editor \(new tab\)/ })).toBeInTheDocument();
    expect(screen.getByText('Enable Resource Pools')).toBeInTheDocument();
    expect(screen.getByText('White Glove')).toBeInTheDocument();
  });

  it('renders validate, dry-run, download YAML, and deploy buttons when schedules loaded', () => {
    render(
      <UploadTab
        dryRun={true}
        schedules={[mockSchedule]}
        setSchedules={noop}
        results={[]}
        setResults={noop}
        showToast={noop}
        onClear={noop}
      />
    );
    expect(screen.getByText('Check prerequisites')).toBeInTheDocument();
    expect(screen.getByText('Preview deployment')).toBeInTheDocument();
    expect(screen.getByText('Download YAML')).toBeInTheDocument();
    expect(screen.getByText('Run dry-run')).toBeInTheDocument();
  });

  it('shows the deploy-target cluster picker for allowlisted operators', async () => {
    vi.spyOn(api, 'getClusters').mockResolvedValue({
      allowed: true,
      user: 'jdisrael@redhat.com',
      clusters: [
        { key: 'events', display_name: 'Events (us-west-2)' },
        { key: 'prod', display_name: 'Prod (us-east-1)' },
      ],
      default: 'events',
    });
    render(
      <UploadTab
        dryRun={true}
        schedules={[mockSchedule]}
        setSchedules={noop}
        results={[]}
        setResults={noop}
        showToast={noop}
        onClear={noop}
      />
    );
    const picker = await screen.findByRole('combobox', { name: 'Deploy target cluster' });
    expect(picker).toBeInTheDocument();
    expect(screen.getByText('Events (us-west-2) (default)')).toBeInTheDocument();
    expect(screen.getByText('Prod (us-east-1)')).toBeInTheDocument();
    // infra01 hosts Flow only (no Babylon) — it is not offered as a deploy target.
    expect(screen.queryByText(/This cluster \(infra01\)/)).not.toBeInTheDocument();
    await waitFor(() => expect(picker).toHaveValue('events'));
  });

  it('shows the picker and defaults to Events even without OAuth identity (Labagator embed)', async () => {
    vi.spyOn(api, 'getClusters').mockResolvedValue({
      allowed: true,
      user: null,
      clusters: [
        { key: 'events', display_name: 'Events (us-west-2)' },
      ],
      default: 'events',
    });
    render(
      <UploadTab
        dryRun={true}
        schedules={[mockSchedule]}
        setSchedules={noop}
        results={[]}
        setResults={noop}
        showToast={noop}
        onClear={noop}
      />
    );
    const picker = await screen.findByRole('combobox', { name: 'Deploy target cluster' });
    expect(picker).toBeInTheDocument();
    await waitFor(() => expect(picker).toHaveValue('events'));
  });

  it('hides the deploy-target cluster picker when no targets are configured', async () => {
    vi.spyOn(api, 'getClusters').mockResolvedValue({
      allowed: false,
      user: null,
      clusters: [],
    });
    render(
      <UploadTab
        dryRun={true}
        schedules={[mockSchedule]}
        setSchedules={noop}
        results={[]}
        setResults={noop}
        showToast={noop}
        onClear={noop}
      />
    );
    await waitFor(() => expect(api.getClusters).toHaveBeenCalled());
    expect(screen.queryByRole('combobox', { name: 'Deploy target cluster' })).not.toBeInTheDocument();
  });

  it('shows "Deploy" button text when not in dry-run mode', () => {
    render(
      <UploadTab
        dryRun={false}
        schedules={[mockSchedule]}
        setSchedules={noop}
        results={[]}
        setResults={noop}
        showToast={noop}
        onClear={noop}
      />
    );
    expect(screen.getByText('Deploy')).toBeInTheDocument();
  });

  it('shows UTC timezone info alert when schedules are loaded', () => {
    render(
      <UploadTab
        dryRun={true}
        schedules={[mockSchedule]}
        setSchedules={noop}
        results={[]}
        setResults={noop}
        showToast={noop}
        onClear={noop}
      />
    );
    expect(screen.getByText(/Schedule times are in UTC/)).toBeInTheDocument();
  });

  it('shows download CSV template link', () => {
    render(
      <UploadTab
        dryRun={true}
        schedules={[mockSchedule]}
        setSchedules={noop}
        results={[]}
        setResults={noop}
        showToast={noop}
        onClear={noop}
      />
    );
    expect(screen.getByText('Download CSV Template')).toBeInTheDocument();
  });

  it('shows search input for filtering schedule preview', () => {
    render(
      <UploadTab
        dryRun={true}
        schedules={[mockSchedule]}
        setSchedules={noop}
        results={[]}
        setResults={noop}
        showToast={noop}
        onClear={noop}
      />
    );
    expect(screen.getByPlaceholderText('Search schedules...')).toBeInTheDocument();
  });

  it('shows Upload Passwords button', () => {
    render(
      <UploadTab
        dryRun={true}
        schedules={[]}
        setSchedules={noop}
        results={[]}
        setResults={noop}
        showToast={noop}
        onClear={noop}
      />
    );
    expect(screen.getByText('Upload Passwords')).toBeInTheDocument();
  });

  it('shows the resolved cluster CI and its source for a tenant row', () => {
    const tenantSchedule = {
      ...mockSchedule,
      item_type: 'Tenant' as const,
      is_tenant: true,
      detected_cluster_ci: 'ocp4-cluster.prod',
      cluster_ci_source: 'agnosticv' as const,
    };
    render(
      <UploadTab
        dryRun={true}
        schedules={[tenantSchedule]}
        setSchedules={noop}
        results={[]}
        setResults={noop}
        showToast={noop}
        onClear={noop}
      />
    );
    expect(screen.getByText('ocp4-cluster.prod')).toBeInTheDocument();
    expect(screen.getByText('AgnosticV')).toBeInTheDocument();
  });

  it('shows a dash for non-tenant rows in the cluster column', () => {
    render(
      <UploadTab
        dryRun={true}
        schedules={[mockSchedule]}
        setSchedules={noop}
        results={[]}
        setResults={noop}
        showToast={noop}
        onClear={noop}
      />
    );
    const clusterCells = screen.getAllByText('-');
    expect(clusterCells.length).toBeGreaterThan(0);
  });
});
