import { useState, useMemo, useCallback, useEffect } from 'react';
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardTitle,
  Divider,
  Flex,
  FlexItem,
  PageSection,
  Label,
  Switch,
  FormSelect,
  FormSelectOption,
  EmptyState,
  EmptyStateBody,
  SearchInput,
  Split,
  SplitItem,
  ToggleGroup,
  ToggleGroupItem,
  Tooltip,
  FileUpload,
  ExpandableSection,
} from '@patternfly/react-core';
import SearchIcon from '@patternfly/react-icons/dist/esm/icons/search-icon';
import CheckCircleIcon from '@patternfly/react-icons/dist/esm/icons/check-circle-icon';
import ExclamationCircleIcon from '@patternfly/react-icons/dist/esm/icons/exclamation-circle-icon';
import ExclamationTriangleIcon from '@patternfly/react-icons/dist/esm/icons/exclamation-triangle-icon';
import CubesIcon from '@patternfly/react-icons/dist/esm/icons/cubes-icon';
import ExternalLinkAltIcon from '@patternfly/react-icons/dist/esm/icons/external-link-alt-icon';
import { useAutoRefresh } from '../hooks/useAutoRefresh';

import { api, getSelectedTarget } from '../services/api';
import { AUTO_REFRESH_INTERVAL_MS, DEFAULT_PER_PAGE } from '../constants';
import type { QAResult, WorkshopSchedule } from '../types';
import { QAResultsTable } from './QAResultsTable';
import { DestroyQASection } from './DestroyQASection';

interface Props {
  qaResults: QAResult[];
  setQAResults: (r: QAResult[]) => void;
  showToast: (msg: string, variant: 'success' | 'danger' | 'info') => void;
  schedules?: WorkshopSchedule[];
}

type SortableQAColumn = 'ci_name' | 'ci' | 'status';
type QAStatusFilter = 'all' | 'verified' | 'failed' | 'unhealthy';

const ALL_NAMESPACES = '__all__';

function isVerified(status: string): boolean {
  const s = (status || '').toLowerCase();
  return s.includes('verified') && !s.includes('unverified');
}

function isFailed(status: string): boolean {
  const s = (status || '').toLowerCase();
  return s.includes('failed') || s.includes('error');
}

function isUnhealthy(r: QAResult): boolean {
  const h = r.healthy;
  return h === false || h === 'No' || h === 'no';
}

function StatusCard({
  icon: Icon,
  color,
  count,
  label,
  tooltip,
  onClick,
  active,
}: {
  icon: React.ComponentType<{ style?: React.CSSProperties }>;
  color: string;
  count: number;
  label: string;
  tooltip: string;
  onClick?: () => void;
  active?: boolean;
}) {
  return (
    <Tooltip content={tooltip}>
      <Card
        isCompact
        isPlain
        onClick={onClick}
        style={{
          cursor: onClick ? 'pointer' : undefined,
          ...(active ? { outline: `2px solid ${color}`, outlineOffset: 2 } : {}),
        }}
      >
        <CardBody>
          <div className="summary-card-value" style={{ color }}>
            <Icon style={{ marginRight: 4 }} />
            {count}
          </div>
          <div className="summary-card-label">{label}</div>
        </CardBody>
      </Card>
    </Tooltip>
  );
}

export const QATab: React.FC<Props> = ({
  qaResults,
  setQAResults,
  showToast,
  schedules = [],
}) => {
  const scheduleNamespaces = useMemo(
    () => [...new Set(schedules.map((s) => s.namespace).filter(Boolean))],
    [schedules],
  );

  const [qaType, setQaType] = useState<'1' | '2' | '3' | 'both' | 'all'>('all');
  const [runNamespace, setRunNamespace] = useState<string>(() =>
    scheduleNamespaces.length === 1 ? scheduleNamespaces[0] : ALL_NAMESPACES,
  );
  const [running, setRunning] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [groupByNamespace, setGroupByNamespace] = useState(
    () => scheduleNamespaces.length > 1,
  );
  const [csvFile, setCsvFile] = useState<File | null>(null);
  const [csvUploading, setCsvUploading] = useState(false);
  const [showAdhoc, setShowAdhoc] = useState(false);
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(DEFAULT_PER_PAGE);
  const [sortBy, setSortBy] = useState<SortableQAColumn | null>('ci_name');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc');
  const [qaSearch, setQaSearch] = useState('');
  const [qaStatusFilter, setQaStatusFilter] = useState<QAStatusFilter>('all');
  const [viewNamespace, setViewNamespace] = useState<string>(ALL_NAMESPACES);
  const [targetCluster, setTargetCluster] = useState(() => getSelectedTarget());

  // Keep run scope in sync when schedules load / change (prefer single NS)
  useEffect(() => {
    if (scheduleNamespaces.length === 1) {
      setRunNamespace(scheduleNamespaces[0]);
      setGroupByNamespace(false);
    } else if (scheduleNamespaces.length > 1) {
      setRunNamespace((prev) =>
        prev !== ALL_NAMESPACES && scheduleNamespaces.includes(prev)
          ? prev
          : ALL_NAMESPACES,
      );
      setGroupByNamespace(true);
    }
  }, [scheduleNamespaces]);

  useEffect(() => {
    const sync = () => setTargetCluster(getSelectedTarget());
    window.addEventListener('rhdp-target-change', sync);
    return () => window.removeEventListener('rhdp-target-change', sync);
  }, []);

  const refreshQA = useCallback(async () => {
    try {
      const data = await api.qaResults();
      setQAResults(data.results);
    } catch (e) {
      console.warn('Auto-refresh QA results failed', e);
    }
  }, [setQAResults]);

  useAutoRefresh(refreshQA, AUTO_REFRESH_INTERVAL_MS, autoRefresh);

  const workshopsInScope = useMemo(() => {
    if (runNamespace === ALL_NAMESPACES) return schedules.length;
    return schedules.filter((s) => s.namespace === runNamespace).length;
  }, [schedules, runNamespace]);

  const runScopeLabel =
    runNamespace === ALL_NAMESPACES
      ? scheduleNamespaces.length > 0
        ? `all ${scheduleNamespaces.length} namespace(s)`
        : 'loaded schedules'
      : runNamespace;

  const handleRun = async () => {
    setRunning(true);
    try {
      const body: Parameters<typeof api.runQA>[0] = { type: qaType };
      if (runNamespace !== ALL_NAMESPACES) {
        body.namespaces = [runNamespace];
      }
      const data = await api.runQA(body);
      setQAResults(data.results);
      setViewNamespace(ALL_NAMESPACES);
      setQaStatusFilter('all');
      setPage(1);
      showToast(
        `QA complete: ${data.count} result(s) for ${runScopeLabel}`,
        'success',
      );
    } catch (e) {
      showToast(`QA failed: ${e}`, 'danger');
    } finally {
      setRunning(false);
    }
  };

  const handleRefresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    try {
      const data = await api.qaResults();
      setQAResults(data.results);
      showToast(`Refreshed: ${data.count} QA result(s)`, 'success');
    } catch (e) {
      showToast(`Refresh failed: ${e}`, 'danger');
    } finally {
      setRefreshing(false);
    }
  };

  const handleCsvUpload = async (file: File | null) => {
    if (!file) {
      setCsvFile(null);
      return;
    }
    setCsvFile(file);
    setCsvUploading(true);
    try {
      const text = await file.text();
      const lines = text.split('\n').filter((l) => l.trim());
      if (lines.length < 2) {
        showToast('CSV must have header and at least one row', 'danger');
        setCsvUploading(false);
        return;
      }
      const headers = lines[0].toLowerCase().split(',').map((h) => h.trim());
      const nsIdx = headers.indexOf('namespace');
      if (nsIdx === -1) {
        showToast('CSV must have a "Namespace" column', 'danger');
        setCsvUploading(false);
        return;
      }
      const namespaces = Array.from(
        new Set(
          lines
            .slice(1)
            .map((line) => {
              const cells = line.split(',');
              return cells[nsIdx]?.trim();
            })
            .filter((ns): ns is string => !!ns && ns.length > 0),
        ),
      );

      if (namespaces.length === 0) {
        showToast('No valid namespaces found in CSV', 'danger');
        setCsvUploading(false);
        return;
      }

      const data = await api.runQA({ type: qaType, namespaces });
      setQAResults(data.results);
      showToast(
        `QA complete from CSV: ${data.count} result(s) across ${namespaces.length} namespace(s)`,
        'success',
      );
    } catch (e) {
      showToast(`CSV QA failed: ${e}`, 'danger');
    } finally {
      setCsvUploading(false);
    }
  };

  const resultNamespaces = useMemo(
    () => [...new Set(qaResults.map((r) => r.namespace || '').filter(Boolean))].sort(),
    [qaResults],
  );

  const statusCounts = useMemo(() => {
    const counts = {
      total: qaResults.length,
      verified: 0,
      failed: 0,
      unhealthy: 0,
      landing: 0,
    };
    for (const r of qaResults) {
      if (isVerified(r.status)) counts.verified++;
      else if (isFailed(r.status)) counts.failed++;
      if (isUnhealthy(r)) counts.unhealthy++;
      if (r.landing_page_url) counts.landing++;
    }
    return counts;
  }, [qaResults]);

  const filteredQAResults = useMemo(() => {
    let filtered = qaResults;
    if (viewNamespace !== ALL_NAMESPACES) {
      filtered = filtered.filter((r) => (r.namespace || '') === viewNamespace);
    }
    if (qaStatusFilter !== 'all') {
      filtered = filtered.filter((r) => {
        if (qaStatusFilter === 'verified') return isVerified(r.status);
        if (qaStatusFilter === 'failed') return isFailed(r.status);
        if (qaStatusFilter === 'unhealthy') return isUnhealthy(r);
        return true;
      });
    }
    if (qaSearch) {
      const q = qaSearch.toLowerCase();
      filtered = filtered.filter(
        (r) =>
          r.ci_name.toLowerCase().includes(q) ||
          r.ci.toLowerCase().includes(q) ||
          (r.namespace || '').toLowerCase().includes(q) ||
          (r.status || '').toLowerCase().includes(q) ||
          String(r.issues || '').toLowerCase().includes(q),
      );
    }
    return filtered;
  }, [qaResults, qaStatusFilter, qaSearch, viewNamespace]);

  const isQAFiltered =
    qaStatusFilter !== 'all' ||
    qaSearch.length > 0 ||
    viewNamespace !== ALL_NAMESPACES;
  const qaTitle = isQAFiltered
    ? `QA Results (${filteredQAResults.length} of ${qaResults.length})`
    : `QA Results (${qaResults.length})`;

  const handleDownloadFilteredCSV = () => {
    const headers = [
      'CI Name',
      'Namespace',
      'CI',
      'Status',
      'Deployed',
      'Healthy',
      'Expected Seats',
      'Actual Seats',
      'Issues',
      'Landing Page URL',
    ];
    const rows = filteredQAResults.map((r) => {
      const rec = r as QAResult & {
        expected_seats?: unknown;
        actual_seats?: unknown;
        actual_users?: unknown;
      };
      const deployedYes = String(r.deployed || '').trim().toLowerCase() === 'yes';
      const expRaw = rec.expected_users ?? rec.expected_seats;
      const expCsv =
        expRaw === null || expRaw === undefined || expRaw === ''
          ? ''
          : String(expRaw);
      let actCsv = '';
      if (deployedYes) {
        const a = rec.actual_count ?? rec.actual_seats ?? rec.actual_users;
        actCsv = a === null || a === undefined || a === '' ? '' : String(a);
      }
      return [
        r.ci_name,
        r.namespace || '',
        r.ci,
        r.status,
        r.deployed,
        String(r.healthy ?? ''),
        expCsv,
        actCsv,
        String(r.issues || ''),
        r.landing_page_url || '',
      ]
        .map((v) => `"${String(v).replace(/"/g, '""')}"`)
        .join(',');
    });
    const csv = [headers.join(','), ...rows].join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `qa-results-${qaStatusFilter}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const noSchedules = schedules.length === 0;

  return (
    <PageSection>
      <Alert variant="info" isInline isPlain title="When to use QA" style={{ marginBottom: 16 }}>
        Run after deploy to verify workshops in your namespace.
        <strong> QA1</strong> right after deploy (config match).
        <strong> QA2</strong> after provision (~10–30 min) for health, student URLs, and Showroom (when a repo is configured).
        {' '}Uses the Upload &amp; Deploy target
        {targetCluster ? (
          <> (<code>{targetCluster}</code>).</>
        ) : (
          <> (whichever cluster is selected there).</>
        )}
        {' '}Day-2 actions (lock, extend, scale, Showroom cleanup) →{' '}
        <strong>Babylon Admin Ops</strong> in Labagator — not duplicated here.
      </Alert>

      {/* Scope + type + run */}
      <Card isCompact style={{ marginBottom: 16 }}>
        <CardBody>
          <Split hasGutter style={{ alignItems: 'flex-start', flexWrap: 'wrap' }}>
            <SplitItem>
              <label htmlFor="qa-namespace-select" style={{ display: 'block', fontSize: '0.85rem', marginBottom: 4 }}>
                Namespace scope
              </label>
              <FormSelect
                id="qa-namespace-select"
                value={runNamespace}
                onChange={(_e, val) => setRunNamespace(val)}
                aria-label="QA namespace scope"
                isDisabled={noSchedules && scheduleNamespaces.length === 0}
                style={{ width: 280 }}
              >
                <FormSelectOption
                  value={ALL_NAMESPACES}
                  label={
                    scheduleNamespaces.length
                      ? `All namespaces (${scheduleNamespaces.length})`
                      : 'All namespaces (from schedules)'
                  }
                />
                {scheduleNamespaces.map((ns) => (
                  <FormSelectOption
                    key={ns}
                    value={ns}
                    label={`${ns} (${schedules.filter((s) => s.namespace === ns).length})`}
                  />
                ))}
              </FormSelect>
            </SplitItem>
            <SplitItem>
              <label htmlFor="qa-type-select" style={{ display: 'block', fontSize: '0.85rem', marginBottom: 4 }}>
                QA type
              </label>
              <FormSelect
                id="qa-type-select"
                value={qaType}
                onChange={(_e, val) => setQaType(val as typeof qaType)}
                aria-label="QA type"
                className="qa-type-select"
                style={{ width: 240 }}
              >
                <FormSelectOption value="1" label="QA1 - Verify Setup" />
                <FormSelectOption value="2" label="QA2 - Verify Deployment" />
                <FormSelectOption value="3" label="QA3 - Verify Catalog Items" />
                <FormSelectOption value="both" label="Both (QA1 + QA2)" />
                <FormSelectOption value="all" label="All (QA1 + QA2 + QA3)" />
              </FormSelect>
            </SplitItem>
            <SplitItem style={{ paddingTop: 22 }}>
              <Tooltip
                content={
                  noSchedules
                    ? 'Upload a schedule CSV first'
                    : `Scan ${workshopsInScope} workshop(s) in ${runScopeLabel}`
                }
              >
                <Button
                  variant="primary"
                  onClick={handleRun}
                  isDisabled={running || noSchedules}
                  isLoading={running}
                >
                  {runNamespace === ALL_NAMESPACES
                    ? 'Run QA'
                    : `Run QA for ${runNamespace.replace(/^user-/, '').replace(/-redhat-com$/, '')}`}
                </Button>
              </Tooltip>
            </SplitItem>
            <SplitItem style={{ paddingTop: 22 }}>
              <Button
                variant="secondary"
                onClick={handleRefresh}
                isLoading={refreshing}
                isDisabled={refreshing}
              >
                Refresh
              </Button>
            </SplitItem>
            <SplitItem style={{ paddingTop: 26 }}>
              <Tooltip content="Poll for updated QA results every 15 seconds">
                <Switch
                  id="qa-auto-refresh"
                  label="Auto-refresh (15s)"
                  isChecked={autoRefresh}
                  onChange={(_e, checked) => setAutoRefresh(checked)}
                />
              </Tooltip>
            </SplitItem>
          </Split>
          <p className="qa-type-hint" style={{ marginTop: 8, marginBottom: 0 }}>
            {noSchedules && (
              <>Load schedules on Upload &amp; Deploy first, then run QA against your namespace.</>
            )}
            {!noSchedules && qaType === '1' && (
              <>Compares live workshops to your schedule — dates, seats, and config.</>
            )}
            {!noSchedules && qaType === '2' && (
              <>Checks provisioned health, seat counts, student landing URLs, and Showroom (if configured).</>
            )}
            {!noSchedules && qaType === '3' && (
              <>Validates catalog items in the CSV exist on the cluster.</>
            )}
            {!noSchedules && qaType === 'both' && (
              <>Setup verification + deployment checks (one row per workshop).</>
            )}
            {!noSchedules && qaType === 'all' && (
              <>Full suite: setup, deployment status, and catalog validation.</>
            )}
            {!noSchedules && (
              <>
                {' '}
                Scope: <strong>{runScopeLabel}</strong>
                {workshopsInScope > 0 && <> · {workshopsInScope} workshop(s)</>}
              </>
            )}
          </p>
        </CardBody>
      </Card>

      <ExpandableSection
        toggleText="Ad-hoc QA from CSV (namespaces only)"
        isExpanded={showAdhoc}
        onToggle={(_e, expanded) => setShowAdhoc(expanded)}
        style={{ marginBottom: 16 }}
      >
        <Card isCompact>
          <CardBody>
            <Split hasGutter style={{ alignItems: 'center' }}>
              <SplitItem style={{ flexGrow: 1, maxWidth: 400 }}>
                <FileUpload
                  id="qa-csv-upload"
                  type="text"
                  value={csvFile || undefined}
                  filename={csvFile?.name || ''}
                  filenamePlaceholder="Upload CSV with a Namespace column"
                  onFileInputChange={(_e, file) => handleCsvUpload(file)}
                  onClearClick={() => setCsvFile(null)}
                  isLoading={csvUploading}
                  browseButtonText="Browse..."
                  clearButtonText="Clear"
                />
              </SplitItem>
              <SplitItem>
                <Label color="blue" icon={<SearchIcon />}>
                  Ad-hoc
                </Label>
              </SplitItem>
            </Split>
          </CardBody>
        </Card>
      </ExpandableSection>

      {qaResults.length > 0 && (
        <Flex style={{ marginBottom: 16 }} gap={{ default: 'gapMd' }}>
          <FlexItem>
            <StatusCard
              icon={CubesIcon}
              color="var(--pf-t--global--text--color--regular)"
              count={statusCounts.total}
              label="Total"
              tooltip="All QA result rows"
              onClick={() => {
                setQaStatusFilter('all');
                setViewNamespace(ALL_NAMESPACES);
                setPage(1);
              }}
              active={qaStatusFilter === 'all' && viewNamespace === ALL_NAMESPACES}
            />
          </FlexItem>
          <FlexItem>
            <StatusCard
              icon={CheckCircleIcon}
              color="var(--pf-v6-global--success-color--100)"
              count={statusCounts.verified}
              label="Verified"
              tooltip="Passed QA verification"
              onClick={() => {
                setQaStatusFilter('verified');
                setPage(1);
              }}
              active={qaStatusFilter === 'verified'}
            />
          </FlexItem>
          <FlexItem>
            <StatusCard
              icon={ExclamationCircleIcon}
              color="var(--pf-v6-global--danger-color--100)"
              count={statusCounts.failed}
              label="Failed"
              tooltip="Failed QA checks — see Issues column"
              onClick={() => {
                setQaStatusFilter('failed');
                setPage(1);
              }}
              active={qaStatusFilter === 'failed'}
            />
          </FlexItem>
          <FlexItem>
            <StatusCard
              icon={ExclamationTriangleIcon}
              color="var(--pf-v6-global--warning-color--100)"
              count={statusCounts.unhealthy}
              label="Unhealthy"
              tooltip="Deployed but health check failed"
              onClick={() => {
                setQaStatusFilter('unhealthy');
                setPage(1);
              }}
              active={qaStatusFilter === 'unhealthy'}
            />
          </FlexItem>
          <FlexItem>
            <StatusCard
              icon={ExternalLinkAltIcon}
              color="var(--pf-v6-global--info-color--100)"
              count={statusCounts.landing}
              label="Landing URLs"
              tooltip="Rows with a student landing page URL (also on Students tab)"
            />
          </FlexItem>
        </Flex>
      )}

      {qaResults.length > 0 && (
        <Split hasGutter style={{ marginBottom: 16, alignItems: 'center', flexWrap: 'wrap' }}>
          <SplitItem>
            <Button
              variant="secondary"
              onClick={handleDownloadFilteredCSV}
              isDisabled={filteredQAResults.length === 0}
            >
              Download{isQAFiltered ? ' Filtered' : ''} CSV
            </Button>
          </SplitItem>
          {resultNamespaces.length > 1 && (
            <SplitItem>
              <FormSelect
                value={viewNamespace}
                onChange={(_e, val) => {
                  setViewNamespace(val);
                  setPage(1);
                }}
                aria-label="Filter QA results by namespace"
                style={{ width: 260 }}
              >
                <FormSelectOption value={ALL_NAMESPACES} label="All namespaces (view)" />
                {resultNamespaces.map((ns) => (
                  <FormSelectOption key={ns} value={ns} label={ns} />
                ))}
              </FormSelect>
            </SplitItem>
          )}
          <SplitItem>
            <Tooltip content="Group results by namespace with collapsible sections">
              <Switch
                id="qa-group-by-namespace"
                label="Group by Namespace"
                isChecked={groupByNamespace}
                onChange={(_e, checked) => setGroupByNamespace(checked)}
              />
            </Tooltip>
          </SplitItem>
          <SplitItem isFilled />
          <SplitItem>
            <SearchInput
              placeholder="Search workshop, CI, namespace, issues..."
              value={qaSearch}
              onChange={(_e, val) => {
                setQaSearch(val);
                setPage(1);
              }}
              onClear={() => {
                setQaSearch('');
                setPage(1);
              }}
              style={{ width: 280 }}
            />
          </SplitItem>
          <SplitItem>
            <ToggleGroup aria-label="QA status filter">
              <ToggleGroupItem
                buttonId="qa-filter-all"
                text="All"
                isSelected={qaStatusFilter === 'all'}
                onChange={() => {
                  setQaStatusFilter('all');
                  setPage(1);
                }}
              />
              <ToggleGroupItem
                buttonId="qa-filter-verified"
                text="Verified"
                isSelected={qaStatusFilter === 'verified'}
                onChange={() => {
                  setQaStatusFilter('verified');
                  setPage(1);
                }}
              />
              <ToggleGroupItem
                buttonId="qa-filter-failed"
                text="Failed"
                isSelected={qaStatusFilter === 'failed'}
                onChange={() => {
                  setQaStatusFilter('failed');
                  setPage(1);
                }}
              />
              <ToggleGroupItem
                buttonId="qa-filter-unhealthy"
                text="Unhealthy"
                isSelected={qaStatusFilter === 'unhealthy'}
                onChange={() => {
                  setQaStatusFilter('unhealthy');
                  setPage(1);
                }}
              />
            </ToggleGroup>
          </SplitItem>
        </Split>
      )}

      {qaResults.length === 0 && (
        <div className="ops-grid" style={{ marginBottom: 16 }}>
          <Card isCompact>
            <CardTitle>QA1 — Verify Setup</CardTitle>
            <CardBody style={{ fontSize: '0.85rem' }}>
              <p>
                <strong>When:</strong> Immediately after deploying workshops.
              </p>
              <p>
                <strong>What it checks:</strong>
              </p>
              <ul style={{ margin: '4px 0 0', paddingLeft: 20 }}>
                <li>Workshop resources exist in the namespace</li>
                <li>Dates and seat counts match the schedule</li>
                <li>Workshop interface settings match</li>
              </ul>
            </CardBody>
          </Card>
          <Card isCompact>
            <CardTitle>QA2 — Verify Deployment</CardTitle>
            <CardBody style={{ fontSize: '0.85rem' }}>
              <p>
                <strong>When:</strong> 10–30 minutes after deploy.
              </p>
              <p>
                <strong>What it checks:</strong>
              </p>
              <ul style={{ margin: '4px 0 0', paddingLeft: 20 }}>
                <li>Health and provisioned seat counts</li>
                <li>Student landing page URLs (Students tab)</li>
              </ul>
            </CardBody>
          </Card>
        </div>
      )}

      {qaResults.length > 0 ? (
        <QAResultsTable
          qaResults={filteredQAResults}
          title={qaTitle}
          page={page}
          setPage={setPage}
          perPage={perPage}
          setPerPage={setPerPage}
          sortBy={sortBy}
          setSortBy={setSortBy}
          sortDir={sortDir}
          setSortDir={setSortDir}
          groupByNamespace={groupByNamespace}
        />
      ) : (
        <EmptyState titleText="No QA results yet" headingLevel="h3" icon={SearchIcon}>
          <EmptyStateBody>
            {noSchedules
              ? 'Upload a schedule on Upload & Deploy, then pick your namespace and click Run QA.'
              : `Select a namespace (you have ${scheduleNamespaces.join(', ') || 'schedules loaded'}) and click Run QA.`}
          </EmptyStateBody>
        </EmptyState>
      )}

      <Divider style={{ margin: '24px 0' }} />
      <DestroyQASection showToast={showToast} />
    </PageSection>
  );
};
