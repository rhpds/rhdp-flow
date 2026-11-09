import { useState, useMemo, useCallback, useEffect } from 'react';
import {
  Alert,
  Button,
  PageSection,
  Title,
  Split,
  SplitItem,
  EmptyState,
  EmptyStateBody,
  Card,
  CardBody,
  Flex,
  FlexItem,
  Pagination,
  SearchInput,
  Switch,
  ToggleGroup,
  ToggleGroupItem,
  Tooltip,
  Label,
} from '@patternfly/react-core';
import { Table, Thead, Tbody, Tr, Th, Td, ThProps } from '@patternfly/react-table';
import CubesIcon from '@patternfly/react-icons/dist/esm/icons/cubes-icon';
import CheckCircleIcon from '@patternfly/react-icons/dist/esm/icons/check-circle-icon';
import ExclamationTriangleIcon from '@patternfly/react-icons/dist/esm/icons/exclamation-triangle-icon';
import ExclamationCircleIcon from '@patternfly/react-icons/dist/esm/icons/exclamation-circle-icon';
import CopyIcon from '@patternfly/react-icons/dist/esm/icons/copy-icon';
import RedoIcon from '@patternfly/react-icons/dist/esm/icons/redo-icon';
import TrashIcon from '@patternfly/react-icons/dist/esm/icons/trash-icon';
import ExternalLinkAltIcon from '@patternfly/react-icons/dist/esm/icons/external-link-alt-icon';

import { api, clearApiCache } from '../services/api';
import { generateServiceLinks } from '../utils/serviceLinks';
import { useAutoRefresh } from '../hooks/useAutoRefresh';
import { AUTO_REFRESH_INTERVAL_MS, DEFAULT_PER_PAGE, RETRY_DELAY_MS } from '../constants';
import { getStatusIndicator, STATUS_LABEL_TO_PF_COLOR } from '../utils/statusColors';
import type { DeploymentResult, OperatorOverride } from '../types';

interface ServiceLinkButtonProps {
  href?: string;
  label: string;
  icon?: React.ComponentType<any>;
}

const ServiceLinkButton: React.FC<ServiceLinkButtonProps> = ({ href, label, icon: Icon = ExternalLinkAltIcon }) => {
  if (!href) return null;
  return (
    <Button
      component="a"
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      variant="link"
      icon={<Icon />}
      iconPosition="right"
      isInline
      style={{ fontSize: '0.875rem', padding: '0 0.5rem' }}
    >
      {label}
    </Button>
  );
};

interface ServiceLinksCellProps {
  result: DeploymentResult;
}

const ServiceLinksCell: React.FC<ServiceLinksCellProps> = ({ result }) => {
  const links = generateServiceLinks(
    result.namespace,
    result.guid,
    result.url,
    result.showroom_url
  );

  return (
    <Flex direction={{ default: 'column' }} spaceItems={{ default: 'spaceItemsXs' }}>
      <ServiceLinkButton href={links.workshop} label="Workshop" />
      <ServiceLinkButton href={links.showroom} label="Showroom" />
      <ServiceLinkButton href={links.openshiftConsole} label="Console" />
      <ServiceLinkButton href={links.resourceClaim} label="Claim" />
    </Flex>
  );
};

interface StatusBadgeProps {
  status: string;
}

const StatusBadge: React.FC<StatusBadgeProps> = ({ status }) => {
  const indicator = getStatusIndicator(status);
  const Icon = indicator.icon;

  return (
    <Label
      color={STATUS_LABEL_TO_PF_COLOR[indicator.label] || 'grey'}
      icon={<Icon />}
    >
      {indicator.label}
    </Label>
  );
};

interface StatusCardProps {
  icon: React.ComponentType<any>;
  color: string;
  count: number;
  label: string;
  tooltip: string;
}

const StatusCard: React.FC<StatusCardProps> = ({ icon: Icon, color, count, label, tooltip }) => (
  <Tooltip content={tooltip}>
    <Card isCompact isPlain>
      <CardBody>
        <Flex direction={{ default: 'column' }} spaceItems={{ default: 'spaceItemsSm' }}>
          <FlexItem>
            <Title headingLevel="h4" size="lg">
              <Icon style={{ color, marginRight: '0.5rem' }} />
              {count}
            </Title>
          </FlexItem>
          <FlexItem>{label}</FlexItem>
        </Flex>
      </CardBody>
    </Card>
  </Tooltip>
);

function isFailed(status: string): boolean {
  const s = (status || '').toLowerCase();
  return s.includes('failed') || s.includes('error');
}

interface Props {
  results: DeploymentResult[];
  setResults: (r: DeploymentResult[]) => void;
  showToast: (msg: string, variant: 'success' | 'danger' | 'info') => void;
  deployLogFile?: string | null;
  operatorOverrides?: OperatorOverride[];
  onOverridesChange?: (overrides: OperatorOverride[]) => void;
  viewingSession?: boolean;
}

type StatusFilter = 'all' | 'verified' | 'unverified' | 'failed';

type SortableColumn = 'ci_name' | 'ci' | 'namespace' | 'status' | 'timestamp';

export const DeploymentsTab: React.FC<Props> = ({
  results,
  setResults,
  showToast,
  deployLogFile,
  operatorOverrides = [],
  onOverridesChange,
  viewingSession = false,
}) => {
  const [searchText, setSearchText] = useState(() => sessionStorage.getItem('rhdp-deploy-search') || '');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>(() => (sessionStorage.getItem('rhdp-deploy-filter') as StatusFilter) || 'all');
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(DEFAULT_PER_PAGE);
  const [sortBy, setSortBy] = useState<SortableColumn | null>(null);
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc');
  const [selectedRows, setSelectedRows] = useState<Set<string>>(new Set());
  const [retrying, setRetrying] = useState(false);
  const [deletingKey, setDeletingKey] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [overridesExpanded, setOverridesExpanded] = useState(false);

  // Persist search and filter to sessionStorage
  useEffect(() => { sessionStorage.setItem('rhdp-deploy-search', searchText); }, [searchText]);
  useEffect(() => { sessionStorage.setItem('rhdp-deploy-filter', statusFilter); }, [statusFilter]);

  const refreshResults = useCallback(async () => {
    try {
      const r = await api.deployResults();
      setResults(r);
    } catch (e) { console.warn('Auto-refresh deploy results failed', e); }
  }, [setResults]);

  useAutoRefresh(refreshResults, AUTO_REFRESH_INTERVAL_MS, autoRefresh);

  // Load persisted deploy results on mount so a browser refresh shows them
  // without requiring a manual Refresh click (results live server-side).
  useEffect(() => {
    refreshResults();
  }, [refreshResults]);

  // Refresh operator override audit (current session only — archived sessions pass props).
  useEffect(() => {
    if (viewingSession || !onOverridesChange) return;
    api.getOperatorOverrides()
      .then((list) => onOverridesChange(Array.isArray(list) ? list : []))
      .catch(() => {});
  }, [viewingSession, onOverridesChange, results.length]);

  // Summary counts
  const statusCounts = useMemo(() => {
    const counts = { total: results.length, verified: 0, unverified: 0, failed: 0 };
    for (const r of results) {
      const s = (r.status || '').toLowerCase();
      if (s.includes('verified') && !s.includes('unverified')) counts.verified++;
      else if (s.includes('failed') || s.includes('error')) counts.failed++;
      else counts.unverified++;
    }
    return counts;
  }, [results]);

  // Filtered and sorted results
  const filteredResults = useMemo(() => {
    let filtered = results;
    if (statusFilter !== 'all') {
      filtered = filtered.filter(r => {
        const s = (r.status || '').toLowerCase();
        if (statusFilter === 'verified') return s.includes('verified') && !s.includes('unverified');
        if (statusFilter === 'failed') return s.includes('failed') || s.includes('error');
        return !(s.includes('verified') && !s.includes('unverified')) && !(s.includes('failed') || s.includes('error'));
      });
    }
    if (searchText) {
      const q = searchText.toLowerCase();
      filtered = filtered.filter(r =>
        r.ci_name.toLowerCase().includes(q) ||
        r.ci.toLowerCase().includes(q) ||
        r.guid.toLowerCase().includes(q) ||
        r.namespace.toLowerCase().includes(q)
      );
    }
    if (sortBy) {
      filtered = [...filtered].sort((a, b) => {
        const aVal = (a[sortBy] || '').toLowerCase();
        const bVal = (b[sortBy] || '').toLowerCase();
        const cmp = aVal.localeCompare(bVal);
        return sortDir === 'asc' ? cmp : -cmp;
      });
    }
    return filtered;
  }, [results, statusFilter, searchText, sortBy, sortDir]);

  // Paginated results
  const paginatedResults = useMemo(() => {
    const start = (page - 1) * perPage;
    return filteredResults.slice(start, start + perPage);
  }, [filteredResults, page, perPage]);

  const handleRefresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    try {
      const r = await api.deployResults();
      setResults(r);
      showToast(`Refreshed: ${r.length} result(s)`, 'success');
    } catch (e) {
      showToast(`Refresh failed: ${e}`, 'danger');
    } finally {
      setRefreshing(false);
    }
  };

  const handleClearAll = async () => {
    if (results.length === 0) return;
    if (!window.confirm(`Remove all ${results.length} deployment result(s) from the dashboard?\n\nThis only clears the display — it does NOT destroy any deployments.`)) return;
    setClearing(true);
    try {
      const r = await api.clearResults();
      setResults([]);
      clearApiCache();
      showToast(`Cleared ${r.cleared} deployment result(s)`, 'success');
    } catch (e) {
      showToast(`Clear failed: ${e}`, 'danger');
    } finally {
      setClearing(false);
    }
  };

  const handleRetry = async (ciNames: string[]) => {
    if (ciNames.length === 0) return;
    setRetrying(true);
    try {
      await api.retry({ ci_names: ciNames });
      showToast(`Retrying ${ciNames.length} deployment(s)...`, 'info');
      // Refresh results after a short delay
      setTimeout(async () => {
        try {
          const r = await api.deployResults();
          setResults(r);
          setSelectedRows(new Set());
        } catch (e) { console.warn('Refresh after retry failed', e); }
        setRetrying(false);
      }, RETRY_DELAY_MS);
    } catch (e) {
      showToast(`Retry failed: ${e}`, 'danger');
      setRetrying(false);
    }
  };

  const handleRetrySelected = () => {
    handleRetry(Array.from(selectedRows));
  };

  const handleDelete = async (r: DeploymentResult) => {
    // View-only cleanup — removes the row from the dashboard, does not undeploy.
    if (!window.confirm(`Remove "${r.ci_name}" (${r.namespace}) from the results list?\n\nThis only clears the dashboard row — it does NOT destroy the deployment.`)) return;
    const key = `${r.ci} ${r.namespace}`;
    setDeletingKey(key);
    try {
      await api.deleteResults([{ ci: r.ci, namespace: r.namespace }]);
      // Remove locally rather than refetch — deployResults is cached (5s TTL),
      // so an immediate refetch could return the pre-delete list. setResults takes
      // an array (not a functional updater), so filter the current results prop.
      setResults(results.filter(x => !(x.ci === r.ci && x.namespace === r.namespace)));
      clearApiCache();
      showToast(`Removed ${r.ci_name} from results`, 'success');
    } catch (e) {
      showToast(`Delete failed: ${e}`, 'danger');
    } finally {
      setDeletingKey(null);
    }
  };

  const toggleRow = (ciName: string) => {
    setSelectedRows(prev => {
      const next = new Set(prev);
      if (next.has(ciName)) next.delete(ciName); else next.add(ciName);
      return next;
    });
  };

  const toggleAllOnPage = () => {
    const pageNames = paginatedResults.map(r => r.ci_name);
    const allSelected = pageNames.every(n => selectedRows.has(n));
    if (allSelected) {
      setSelectedRows(prev => {
        const next = new Set(prev);
        pageNames.forEach(n => next.delete(n));
        return next;
      });
    } else {
      setSelectedRows(prev => {
        const next = new Set(prev);
        pageNames.forEach(n => next.add(n));
        return next;
      });
    }
  };

  const selectAllFiltered = () => {
    setSelectedRows(new Set(filteredResults.map(r => r.ci_name)));
  };

  const allPageSelected = paginatedResults.length > 0 && paginatedResults.every(r => selectedRows.has(r.ci_name));
  const allFilteredSelected = filteredResults.length > 0 && filteredResults.every(r => selectedRows.has(r.ci_name));
  const showSelectAllBanner = allPageSelected && !allFilteredSelected && filteredResults.length > perPage;

  const getSortParams = (col: SortableColumn): ThProps['sort'] => ({
    sortBy: sortBy === col ? { index: 0, direction: sortDir } : { index: 0, direction: 'asc', defaultDirection: 'asc' },
    onSort: () => {
      if (sortBy === col) {
        setSortDir(prev => prev === 'asc' ? 'desc' : 'asc');
      } else {
        setSortBy(col);
        setSortDir('asc');
      }
      setPage(1);
    },
    columnIndex: 0,
  });

  const isFiltered = statusFilter !== 'all' || searchText.length > 0;
  const titleText = isFiltered
    ? `Deployment Results (${filteredResults.length} of ${results.length})`
    : `Deployment Results (${results.length})`;

  return (
    <PageSection>
      {operatorOverrides.length > 0 && (
        <Alert
          variant="warning"
          isInline
          title={`${operatorOverrides.length} operator override(s) this session — Labagator was not the sole source of truth`}
          style={{ marginBottom: 16 }}
          actionLinks={
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <Button variant="link" size="sm" onClick={() => setOverridesExpanded((v) => !v)}>
                {overridesExpanded ? 'Hide details' : 'Show details'}
              </Button>
              {!viewingSession && (
                <Button
                  variant="link"
                  size="sm"
                  onClick={async () => {
                    try {
                      await api.clearOperatorOverrides();
                      onOverridesChange?.([]);
                      showToast('Cleared override audit list (schedules unchanged)', 'info');
                    } catch (e) {
                      showToast(`Clear overrides failed: ${e}`, 'danger');
                    }
                  }}
                >
                  Clear audit list
                </Button>
              )}
            </div>
          }
        >
          <div style={{ fontSize: '0.9rem', marginBottom: overridesExpanded ? 8 : 0 }}>
            These were local Flow tweaks the operator accepted (Users→Instances, skip missing CIs, −4h, etc.).
            If a deploy looks wrong vs Labagator, check this list and the deploy log first — do not blame Flow defaults.
          </div>
          {overridesExpanded && (
            <ul style={{ margin: '0 0 0 20px', fontSize: '0.85rem' }}>
              {operatorOverrides.map((o, i) => (
                <li key={`${o.timestamp}-${o.action}-${i}`}>
                  <strong>{o.action}</strong>
                  {o.timestamp ? (
                    <span style={{ color: 'var(--pf-v6-global--Color--200)' }}> · {o.timestamp}</span>
                  ) : null}
                  {' — '}
                  {o.summary}
                  {o.affected_count > 0 ? ` (${o.affected_count} row(s))` : ''}
                  {o.detail ? (
                    <div style={{ color: 'var(--pf-v6-global--Color--200)', fontSize: '0.8rem' }}>{o.detail}</div>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </Alert>
      )}

      {/* Summary cards */}
      {results.length > 0 && (
        <Flex style={{ marginBottom: 16 }} gap={{ default: 'gapMd' }}>
          <FlexItem>
            <Tooltip content="Total number of deployment results across all statuses">
              <Card isCompact isPlain>
                <CardBody>
                  <div className="summary-card-value"><CubesIcon style={{ marginRight: 4 }} />{statusCounts.total}</div>
                  <div className="summary-card-label">Total</div>
                </CardBody>
              </Card>
            </Tooltip>
          </FlexItem>
          <FlexItem>
            <StatusCard
              icon={CheckCircleIcon}
              color="var(--pf-v6-global--success-color--100)"
              count={statusCounts.verified}
              label="Verified"
              tooltip="Deployed and confirmed healthy via QA verification"
            />
          </FlexItem>
          <FlexItem>
            <StatusCard
              icon={ExclamationTriangleIcon}
              color="var(--pf-v6-global--warning-color--100)"
              count={statusCounts.unverified}
              label="Unverified"
              tooltip="Deployed successfully but not yet verified by QA checks"
            />
          </FlexItem>
          <FlexItem>
            <StatusCard
              icon={ExclamationCircleIcon}
              color="var(--pf-v6-global--danger-color--100)"
              count={statusCounts.failed}
              label="Failed"
              tooltip="Deployment encountered an error — check the Error column for details"
            />
          </FlexItem>
        </Flex>
      )}

      {/* Toolbar */}
      <Split hasGutter style={{ marginBottom: 16, alignItems: 'center' }}>
        <SplitItem>
          <Title headingLevel="h3">{titleText}</Title>
        </SplitItem>
        <SplitItem isFilled />
        <SplitItem>
          <SearchInput
            placeholder="Search CI, GUID, namespace..."
            value={searchText}
            onChange={(_e, val) => { setSearchText(val); setPage(1); }}
            onClear={() => { setSearchText(''); setPage(1); }}
            style={{ width: 250 }}
          />
        </SplitItem>
        <SplitItem>
          <ToggleGroup aria-label="Status filter">
            <ToggleGroupItem buttonId="deploy-filter-all" text="All" isSelected={statusFilter === 'all'} onChange={() => { setStatusFilter('all'); setPage(1); }} />
            <ToggleGroupItem buttonId="deploy-filter-verified" text="Verified" isSelected={statusFilter === 'verified'} onChange={() => { setStatusFilter('verified'); setPage(1); }} />
            <ToggleGroupItem buttonId="deploy-filter-unverified" text="Unverified" isSelected={statusFilter === 'unverified'} onChange={() => { setStatusFilter('unverified'); setPage(1); }} />
            <ToggleGroupItem buttonId="deploy-filter-failed" text="Failed" isSelected={statusFilter === 'failed'} onChange={() => { setStatusFilter('failed'); setPage(1); }} />
          </ToggleGroup>
        </SplitItem>
      </Split>

      <Split hasGutter style={{ marginBottom: 16, alignItems: 'center' }}>
        <SplitItem>
          <Tooltip content="Automatically poll for updated deployment results every 15 seconds">
            <Switch
              id="auto-refresh-switch"
              label="Auto-refresh (15s)"
              isChecked={autoRefresh}
              onChange={(_e, checked) => setAutoRefresh(checked)}
            />
          </Tooltip>
        </SplitItem>
        <SplitItem isFilled />
        <SplitItem>
          <Button variant="secondary" onClick={handleRefresh} isLoading={refreshing} isDisabled={refreshing}>Refresh</Button>
        </SplitItem>
        <SplitItem>
          <Tooltip content="Remove all deployment results from the dashboard (does not destroy deployments)">
            <Button
              variant="danger"
              onClick={handleClearAll}
              isLoading={clearing}
              isDisabled={clearing || results.length === 0}
            >
              Clear Deployments
            </Button>
          </Tooltip>
        </SplitItem>
        <SplitItem>
          <Button
            variant="secondary"
            component="a"
            href={api.exportResultsURL}
            isDisabled={results.length === 0}
          >
            Download CSV
          </Button>
        </SplitItem>
        <SplitItem>
          <Button
            variant="secondary"
            onClick={async () => {
              try {
                const response = await fetch('/api/schedules/export-for-labagator');
                const blob = await response.blob();
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = 'flow-export-for-labagator.csv';
                document.body.appendChild(a);
                a.click();
                window.URL.revokeObjectURL(url);
                document.body.removeChild(a);
                showToast('Exported for Labagator', 'success');
              } catch (e) {
                showToast('Export failed', 'danger');
              }
            }}
          >
            Export for Labagator
          </Button>
        </SplitItem>
        <SplitItem>
          <Button
            variant="secondary"
            component="a"
            href={deployLogFile ? api.logURL(deployLogFile) : '#'}
            isDisabled={!deployLogFile}
          >
            Download Log
          </Button>
        </SplitItem>
        {selectedRows.size > 0 && (
          <SplitItem>
            <Button
              variant="warning"
              icon={<RedoIcon />}
              onClick={handleRetrySelected}
              isLoading={retrying}
              isDisabled={retrying}
            >
              Retry Selected ({selectedRows.size})
            </Button>
          </SplitItem>
        )}
      </Split>

      {filteredResults.length > 0 ? (
        <>
          {showSelectAllBanner && (
            <Alert variant="info" isInline isPlain title={`All ${paginatedResults.length} on this page are selected.`} style={{ marginBottom: 8 }}>
              <Button variant="link" isInline onClick={selectAllFiltered}>
                Select all {filteredResults.length} filtered results
              </Button>
            </Alert>
          )}
          {allFilteredSelected && filteredResults.length > perPage && (
            <Alert variant="info" isInline isPlain title={`All ${filteredResults.length} filtered results are selected.`} style={{ marginBottom: 8 }}>
              <Button variant="link" isInline onClick={() => setSelectedRows(new Set())}>
                Clear selection
              </Button>
            </Alert>
          )}
          <div className="table-sticky-wrapper">
          <Table aria-label="Deployment results" variant="compact" className="fixed-table" isStickyHeader>
            <Thead>
              <Tr>
                <Th
                  select={{
                    onSelect: toggleAllOnPage,
                    isSelected: paginatedResults.length > 0 && paginatedResults.every(r => selectedRows.has(r.ci_name)),
                  }}
                />
                <Th sort={getSortParams('ci_name')} info={{ tooltip: 'Catalog Item display name' }}>CI Name</Th>
                <Th sort={getSortParams('ci')} info={{ tooltip: 'Catalog Item identifier (vendor.item.env)' }}>CI</Th>
                <Th sort={getSortParams('namespace')} info={{ tooltip: 'OpenShift namespace where resources are deployed' }}>Namespace</Th>
                <Th info={{ tooltip: 'Seat count (num_users) requested for this deployment' }}>Users</Th>
                <Th info={{ tooltip: 'WorkshopProvision instance count requested for this deployment' }}>Instances</Th>
                <Th info={{ tooltip: 'Globally Unique Identifier for this deployment instance' }}>GUID</Th>
                <Th sort={getSortParams('status')}>Status</Th>
                <Th info={{ tooltip: 'Quick access links to workshop, showroom, OpenShift console, and ResourceClaim' }}>Services</Th>
                <Th>URL</Th>
                <Th>Prov. Date (UTC)</Th>
                <Th>Auto-Stop (UTC)</Th>
                <Th>Auto-Destroy (UTC)</Th>
                <Th sort={getSortParams('timestamp')}>Timestamp (UTC)</Th>
                <Th>Error</Th>
                <Th>Actions</Th>
              </Tr>
            </Thead>
            <Tbody>
              {paginatedResults.map((r, i) => (
                <Tr key={`${r.ci_name}-${r.guid}`}>
                  <Td
                    select={{
                      rowIndex: i,
                      onSelect: () => toggleRow(r.ci_name),
                      isSelected: selectedRows.has(r.ci_name),
                    }}
                  />
                  <Td dataLabel="CI Name">{r.ci_name}</Td>
                  <Td dataLabel="CI">{r.ci}</Td>
                  <Td dataLabel="Namespace">{r.namespace}</Td>
                  <Td dataLabel="Users">{r.users ?? '-'}</Td>
                  <Td dataLabel="Instances">{r.instances ?? '-'}</Td>
                  <Td dataLabel="GUID">{r.guid}</Td>
                  <Td dataLabel="Status">
                    <StatusBadge status={r.status} />
                  </Td>
                  <Td dataLabel="Services">
                    <ServiceLinksCell result={r} />
                  </Td>
                  <Td dataLabel="URL">
                    {r.url ? (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                        <a href={r.url} target="_blank" rel="noopener noreferrer" className="cell-truncate" title={r.url}>{r.url}</a>
                        <Tooltip content="Copy URL">
                          <Button
                            variant="plain"
                            size="sm"
                            style={{ padding: '2px 4px' }}
                            onClick={() => navigator.clipboard.writeText(r.url).then(
                              () => showToast('URL copied to clipboard', 'success'),
                              () => showToast('Clipboard access denied', 'danger')
                            )}
                            aria-label="Copy URL"
                          >
                            <CopyIcon />
                          </Button>
                        </Tooltip>
                      </span>
                    ) : '-'}
                  </Td>
                  <Td dataLabel="Prov. Date (UTC)" className="date-cell">{r.provisioning_date}</Td>
                  <Td dataLabel="Auto-Stop (UTC)" className="date-cell">{r.auto_stop || '-'}</Td>
                  <Td dataLabel="Auto-Destroy (UTC)" className="date-cell">{r.auto_destroy || '-'}</Td>
                  <Td dataLabel="Timestamp (UTC)" className="date-cell">{r.timestamp}</Td>
                  <Td dataLabel="Error">{r.error_message || ''}</Td>
                  <Td dataLabel="Actions">
                    {isFailed(r.status) && (
                      <Tooltip content="Retry this deployment">
                        <Button
                          variant="plain"
                          size="sm"
                          onClick={() => handleRetry([r.ci_name])}
                          isDisabled={retrying}
                          aria-label="Retry"
                        >
                          <RedoIcon />
                        </Button>
                      </Tooltip>
                    )}
                    <Tooltip content="Remove this row from the results (does not undeploy)">
                      <Button
                        variant="plain"
                        size="sm"
                        onClick={() => handleDelete(r)}
                        isDisabled={deletingKey === `${r.ci} ${r.namespace}`}
                        aria-label="Delete result"
                      >
                        <TrashIcon />
                      </Button>
                    </Tooltip>
                  </Td>
                </Tr>
              ))}
            </Tbody>
          </Table>
          </div>
          {filteredResults.length > perPage && (
            <Pagination
              itemCount={filteredResults.length}
              perPage={perPage}
              page={page}
              onSetPage={(_e, p) => setPage(p)}
              onPerPageSelect={(_e, pp) => { setPerPage(pp); setPage(1); }}
              perPageOptions={[
                { title: '10', value: 10 },
                { title: '20', value: 20 },
                { title: '50', value: 50 },
                { title: '100', value: 100 },
              ]}
              style={{ marginTop: 8 }}
            />
          )}
        </>
      ) : results.length > 0 ? (
        <EmptyState titleText="No matching results" headingLevel="h3" icon={CubesIcon}>
          <EmptyStateBody>Try adjusting your search or filter criteria.</EmptyStateBody>
        </EmptyState>
      ) : (
        <EmptyState titleText="No deployment results yet" headingLevel="h3" icon={CubesIcon}>
          <EmptyStateBody>Deploy schedules from the Upload tab to see results here.</EmptyStateBody>
        </EmptyState>
      )}
    </PageSection>
  );
};
