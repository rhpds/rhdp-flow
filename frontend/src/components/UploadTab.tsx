import { useState, useRef, useEffect, useCallback, useMemo, Fragment, type ReactNode } from 'react';
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardTitle,
  Checkbox,
  FormSelect,
  FormSelectOption,
  HelperText,
  HelperTextItem,
  Label,
  PageSection,
  Title,
  Progress,
  Split,
  SplitItem,
  Spinner,
  Switch,
  EmptyState,
  EmptyStateBody,
  FileUpload,
  Modal,
  ModalBody,
  ModalHeader,
  ModalFooter,
  SearchInput,
  Tooltip,
  TextInput,
  ExpandableSection,
  NumberInput,
  ToggleGroup,
  ToggleGroupItem,
} from '@patternfly/react-core';
import { Table, Thead, Tbody, Tr, Th, Td, ExpandableRowContent } from '@patternfly/react-table';
import UploadIcon from '@patternfly/react-icons/dist/esm/icons/upload-icon';
import TrashIcon from '@patternfly/react-icons/dist/esm/icons/trash-icon';
import InfoCircleIcon from '@patternfly/react-icons/dist/esm/icons/info-circle-icon';

import { api, getSelectedTarget, selectTargetCluster } from '../services/api';
import { DiffView } from './DiffView';
import { CatalogItemSelect } from './CatalogItemSelect';
import type { WorkshopSchedule, DeploymentResult, NumUsersViolation, UsersNotInCatalogAdvisory, UsersBlankCatalogDefaultAdvisory, ParameterValidationViolation, ScheduleExampleMeta, LabagatorEventSummary, LabagatorPreviewResponse, LabagatorSessionSummary } from '../types';

/* ── Schedule date validation helpers ── */

interface ScheduleWarning {
  index: number;
  /** All schedule rows this warning applies to (for table highlighting). */
  indices?: number[];
  field: string;
  message: string;
  /** Grouped overlap/conflict summaries — shown in the expandable details list. */
  kind?: 'conflict' | 'other';
}

/** Parse DD/MM/YYYY HH:MM (or DD/MM/YY HH:MM) into a Date, or null. Accepts space or colon separator for Labugator compatibility. */
function parseScheduleDate(dateStr: string): Date | null {
  if (!dateStr?.trim()) return null;
  const m = dateStr.trim().match(/^(\d{1,2})\/(\d{1,2})\/(\d{2,4})[\s:]+(\d{1,2}):(\d{2})$/);
  if (m) {
    const yr = m[3].length === 2 ? 2000 + parseInt(m[3]) : parseInt(m[3]);
    return new Date(Date.UTC(yr, parseInt(m[2]) - 1, parseInt(m[1]), parseInt(m[4]), parseInt(m[5])));
  }
  // Fallback: append 'Z' to force UTC parsing (fixes BST/local time bug)
  const utcStr = dateStr.trim() + (dateStr.includes('Z') ? '' : 'Z');
  const d = new Date(utcStr);
  return isNaN(d.getTime()) ? null : d;
}

function shiftScheduleDate(dateStr: string, hours: number): string {
  const d = parseScheduleDate(dateStr);
  if (!d) return dateStr;
  d.setUTCHours(d.getUTCHours() + hours);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getUTCDate())}/${pad(d.getUTCMonth() + 1)}/${d.getUTCFullYear()} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

/** First schedule row per catalog item — avoids duplicate CI lines in tenant alerts. */
function uniqueByCi<T extends { ci: string }>(refs: T[]): T[] {
  const seen = new Set<string>();
  return refs.filter((r) => {
    if (seen.has(r.ci)) return false;
    seen.add(r.ci);
    return true;
  });
}

/** Sort alert rows by CI id, then display name. */
function sortByCiName<T extends { ci?: string; ci_name?: string; workshop_name?: string }>(items: T[]): T[] {
  return [...items].sort((a, b) => {
    const ac = (a.ci || '').localeCompare(b.ci || '');
    if (ac !== 0) return ac;
    return (a.ci_name || a.workshop_name || '').localeCompare(b.ci_name || b.workshop_name || '');
  });
}

/** Compact “Affects N” + expand — no partial lists / “…and N more”. */
function AffectsItemsList({
  count,
  expanded,
  onToggle,
  children,
  noun = 'item',
}: {
  count: number;
  expanded: boolean;
  onToggle: () => void;
  children: ReactNode;
  noun?: string;
}) {
  if (count <= 0) return null;
  const label = count === 1 ? noun : `${noun}s`;
  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ fontSize: '0.9rem' }}>
        Affects <strong>{count}</strong> {label}.{' '}
        <Button variant="link" isInline size="sm" onClick={onToggle}>
          {expanded ? 'Hide items' : 'Show items'}
        </Button>
      </div>
      {expanded && (
        <ul
          style={{
            margin: '6px 0 0 20px',
            paddingLeft: 0,
            fontSize: '0.85rem',
            maxHeight: 240,
            overflowY: 'auto',
          }}
        >
          {children}
        </ul>
      )}
    </div>
  );
}

function clusterCisFromTenantRefs(refs: { ci: string; cluster_ref?: string }[]): string[] {
  return [...new Set(
    refs.map((r) => {
      if (r.cluster_ref) return r.cluster_ref;
      const derived = (r.ci as string).replace(/-tenant\./, '-cluster.');
      return derived !== r.ci ? derived : null;
    }).filter(Boolean),
  )] as string[];
}

/** Client-side CSV download — additive confirmation export only. */
function downloadCsv(filename: string, headers: string[], rows: Array<Array<string | number | boolean | null | undefined>>) {
  const esc = (v: string | number | boolean | null | undefined) => {
    const s = v === null || v === undefined ? '' : String(v);
    if (/[",\n\r]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
    return s;
  };
  const csv = [headers.map(esc).join(','), ...rows.map((r) => r.map(esc).join(','))].join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function stampForFilename(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}${p(d.getUTCMonth() + 1)}${p(d.getUTCDate())}-${p(d.getUTCHours())}${p(d.getUTCMinutes())}Z`;
}

interface Props {
  dryRun: boolean;
  schedules: WorkshopSchedule[];
  setSchedules: (s: WorkshopSchedule[]) => void;
  results: DeploymentResult[];
  setResults: (r: DeploymentResult[]) => void;
  showToast: (msg: string, variant: 'success' | 'danger' | 'info') => void;
  onClear: () => void;
  setDeployLogFile?: (f: string | null) => void;
  onOperatorOverrideRecorded?: () => void;
}

export const UploadTab: React.FC<Props> = ({
  dryRun, schedules, setSchedules, results, setResults, showToast, onClear, setDeployLogFile,
  onOperatorOverrideRecorded,
}) => {
  const logRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const jobIdRef = useRef<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Clean up WebSocket on unmount
  useEffect(() => {
    return () => {
      wsRef.current?.close();
      wsRef.current = null;
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    api.listScheduleExamples()
      .then(setScheduleExamples)
      .catch(() => setScheduleExamples([]));
  }, []);

  // Look-ahead window for the Labagator event list (days; 0 = all upcoming).
  const [labagatorRangeDays, setLabagatorRangeDays] = useState(7);
  const [labagatorEventsLoading, setLabagatorEventsLoading] = useState(false);
  const [labagatorEvents, setLabagatorEvents] = useState<LabagatorEventSummary[]>([]);
  // Session picker: sessions fetched for the selected event + which are checked.
  const [labagatorSessions, setLabagatorSessions] = useState<LabagatorSessionSummary[]>([]);
  const [labagatorSessionsLoading, setLabagatorSessionsLoading] = useState(false);
  const [labagatorSessionsFetched, setLabagatorSessionsFetched] = useState(false);
  const [selectedSessionIds, setSelectedSessionIds] = useState<Set<number>>(new Set());

  const loadLabagatorEvents = useCallback((days: number) => {
    setLabagatorEventsLoading(true);
    api.listLabagatorEvents(days)
      .then((res) => {
        setLabagatorUnavailable(!!res.error);
        setLabagatorEvents(res.events ?? []);
      })
      .catch(() => {
        setLabagatorUnavailable(true);
        setLabagatorEvents([]);
      })
      .finally(() => setLabagatorEventsLoading(false));
  }, []);

  useEffect(() => {
    loadLabagatorEvents(labagatorRangeDays);
    // Refetch whenever the look-ahead range changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [labagatorRangeDays]);

  const [deploying, setDeploying] = useState(false);
  const [deployPaused, setDeployPaused] = useState(false);
  const [validating, setValidating] = useState(false);
  const [yamlDownloading, setYamlDownloading] = useState(false);
  const [rowEditsLocked, setRowEditsLocked] = useState(false);
  const [scheduleExamples, setScheduleExamples] = useState<ScheduleExampleMeta[]>([]);
  const [loadingExampleSlug, setLoadingExampleSlug] = useState<string | null>(null);
  const [passwordCount, setPasswordCount] = useState<number | null>(null);

  // Labagator import state (live API import)
  const [labagatorUnavailable, setLabagatorUnavailable] = useState(false);
  const [selectedEventId, setSelectedEventId] = useState<number | null>(null);
  const [labagatorNamespace, setLabagatorNamespace] = useState('');
  const [labagatorNamespaceError, setLabagatorNamespaceError] = useState('');
  const [labagatorAdvancedOpen, setLabagatorAdvancedOpen] = useState(false);
  const [labagatorEnableWorkshopInterface, setLabagatorEnableWorkshopInterface] = useState(true);
  const [labagatorConcurrency, setLabagatorConcurrency] = useState(10);
  const [labagatorAutoStopDays, setLabagatorAutoStopDays] = useState(7);
  const [labagatorAutoDestroyDays, setLabagatorAutoDestroyDays] = useState(14);
  const [labagatorPreviewing, setLabagatorPreviewing] = useState(false);
  const [labagatorPreview, setLabagatorPreview] = useState<LabagatorPreviewResponse | null>(null);
  const [labagatorImporting, setLabagatorImporting] = useState(false);
  const [showLabagatorConfirm, setShowLabagatorConfirm] = useState(false);

  // Labagator import settings (legacy manual CSV upload)
  const [importMode, setImportMode] = useState<'flow' | 'labagator'>('flow');
  const [labagatorDefaultCI, setLabagatorDefaultCI] = useState('');
  const [labagatorDefaultUsers, setLabagatorDefaultUsers] = useState(25);
  const [labagatorBufferHours, setLabagatorBufferHours] = useState(2);
  const [progress, setProgress] = useState(0);
  const [progressMsg, setProgressMsg] = useState('');
  const [logLines, setLogLines] = useState<string[]>([]);

  // FileUpload state
  const [csvFile, setCsvFile] = useState<File | null>(null);
  const [csvFilename, setCsvFilename] = useState('');
  const [passwordFile, setPasswordFile] = useState<File | null>(null);
  const [passwordFilename, setPasswordFilename] = useState('');

  // Deploy settings
  const [resourceLock, setResourceLock] = useState(true);
  const [enableResourcePools, setEnableResourcePools] = useState(false);
  const [usePoolLookup, setUsePoolLookup] = useState(false);
  const [poolLookupData, setPoolLookupData] = useState<Record<string, import('../types').PoolLookupResponse>>({});
  const [allPools, setAllPools] = useState<import('../types').PoolInfo[]>([]);
  const [whiteGlove, setWhiteGlove] = useState(true);
  const [redirect, setRedirect] = useState(true);
  const [showroomNovnc, setShowroomNovnc] = useState(false);
  const [showroomZerotouch, setShowroomZerotouch] = useState(false);
  const [useCatalogLookup, setUseCatalogLookup] = useState(false);
  // '' = auto pace by batch size (0s / 0.5s / 1s) — create-only scheduling
  const [deployDelaySeconds, setDeployDelaySeconds] = useState('');

  // Multi-cluster deploy target. Always default to Events (us-west-2) when configured.
  // Dropdown is shown whenever targets exist — Labagator embeds use API key only (no OAuth email).
  const DEFAULT_TARGET_CLUSTER = 'events';
  const [deployClusters, setDeployClusters] = useState<import('../types').ClusterTarget[]>([]);
  const [targetCluster, setTargetCluster] = useState<string>(getSelectedTarget);

  useEffect(() => {
    let cancelled = false;
    api.getClusters()
      .then((resp) => {
        if (cancelled) return;
        const clusters = resp.clusters ?? [];
        setDeployClusters(clusters);
        const defaultKey = resp.default || DEFAULT_TARGET_CLUSTER;
        const hasDefault = clusters.some((c) => c.key === defaultKey);
        if (hasDefault && !getSelectedTarget()) {
          selectTargetCluster(defaultKey);
          setTargetCluster(defaultKey);
        }
      })
      .catch(() => {
        if (cancelled) return;
        setDeployClusters([]);
      });
    return () => { cancelled = true; };
  }, []);

  // Namespace validation
  const [missingNamespaces, setMissingNamespaces] = useState<string[]>([]);

  // num_users limit validation
  const [numUsersViolations, setNumUsersViolations] = useState<NumUsersViolation[]>([]);
  const [usersNotInCatalog, setUsersNotInCatalog] = useState<UsersNotInCatalogAdvisory[]>([]);
  const [usersBlankCatalogDefault, setUsersBlankCatalogDefault] = useState<UsersBlankCatalogDefaultAdvisory[]>([]);
  const [usersBlankExpanded, setUsersBlankExpanded] = useState(false);
  const [numUsersLimits, setNumUsersLimits] = useState<Record<string, number>>({});

  // Catalog parameter schema validation (e.g. AWS_Region enum)
  const [paramViolations, setParamViolations] = useState<ParameterValidationViolation[]>([]);
  const [paramWarnings, setParamWarnings] = useState<ParameterValidationViolation[]>([]);
  const [paramViolationsExpanded, setParamViolationsExpanded] = useState(false);
  const [paramWarningsExpanded, setParamWarningsExpanded] = useState(false);

  // Catalog namespace validation
  const [catalogNamespaceMismatches, setCatalogNamespaceMismatches] = useState<import('../types').CatalogNamespaceMismatch[]>([]);
  const [catalogNotFound, setCatalogNotFound] = useState<import('../types').CatalogNotFoundItem[]>([]);
  /** Bare CI → .prod (or single-suffix) auto-corrections applied on validate — like namespace redirect. */
  const [catalogSuffixCorrections, setCatalogSuffixCorrections] = useState<Array<{
    ci: string;
    corrected_ci: string;
    ci_name: string;
    message: string;
  }>>([]);
  /** Non-blocking: rows using .prod instead of .event (nice-to-know for event batches). */
  const [prodNotEvent, setProdNotEvent] = useState<import('../types').ProdNotEventAdvisory[]>([]);
  const [prodNotEventExpanded, setProdNotEventExpanded] = useState(false);
  const [skippedCatalogSummary, setSkippedCatalogSummary] = useState<{
    rows: number;
    remaining: number;
    items: Array<{ ci: string; ci_name: string; reason: string }>;
    removedSchedules: WorkshopSchedule[];
  } | null>(null);

  const [clusterNeeds, setClusterNeeds] = useState<any>(null);
  const [missingTenantRefs, setMissingTenantRefs] = useState<any>(null);

  // Pool capacity validation
  const [poolCapacityWarnings, setPoolCapacityWarnings] = useState<import('../types').PoolCapacityWarning[]>([]);
  const [poolsNotFound, setPoolsNotFound] = useState<import('../types').PoolNotFoundWarning[]>([]);

  // Confirmation modal state
  const [showDeployConfirm, setShowDeployConfirm] = useState(false);
  const [showClearConfirm, setShowClearConfirm] = useState(false);
  /** One shared confirm for riskier local bypasses (skip CIs, rewrite CI suffix, etc.). */
  const [riskConfirm, setRiskConfirm] = useState<{
    kind: 'skip-missing' | 'apply-prod-no-event' | 'apply-event' | 'users-to-instances' | 'clear-users' | 'cap-users';
    title: string;
    body: string;
    bullets: string[];
  } | null>(null);
  const [deployRiskAck, setDeployRiskAck] = useState(false);
  const [catalogNotFoundExpanded, setCatalogNotFoundExpanded] = useState(false);
  const [catalogNsExpanded, setCatalogNsExpanded] = useState(false);
  const [catalogSuffixExpanded, setCatalogSuffixExpanded] = useState(false);
  const [tenantNoPoolExpanded, setTenantNoPoolExpanded] = useState(false);
  const [viaFreshExpanded, setViaFreshExpanded] = useState(false);
  const [modalListsExpanded, setModalListsExpanded] = useState(false);
  const [usersAdvisoryExpanded, setUsersAdvisoryExpanded] = useState(false);
  const [numUsersExpanded, setNumUsersExpanded] = useState(false);
  const [skippedItemsExpanded, setSkippedItemsExpanded] = useState(false);
  const [poolWarnExpanded, setPoolWarnExpanded] = useState(false);

  // Expandable rows state
  const [expandedRows, setExpandedRows] = useState<Set<number>>(new Set());

  // Search filter for schedule preview
  const [previewSearch, setPreviewSearch] = useState('');

  // Search filter for pool override dropdowns (per-row)
  const [poolSearchFilters, setPoolSearchFilters] = useState<Record<number, string>>({});

  // Skipped row tracking (CSV parse)
  const [skippedRows, setSkippedRows] = useState<number>(0);
  const [totalRows, setTotalRows] = useState<number>(0);

  // Catalog namespace bulk override modal
  const [showCatalogOverrideModal, setShowCatalogOverrideModal] = useState(false);
  const [catalogOverrideAction, setCatalogOverrideAction] = useState<'event' | 'prod' | 'dev' | 'clear' | null>(null);

  // Fill missing dates modal
  const [showFillDatesModal, setShowFillDatesModal] = useState(false);
  const [fillProvDate, setFillProvDate] = useState('');
  const [fillStopDate, setFillStopDate] = useState('');
  const [fillDestroyDate, setFillDestroyDate] = useState('');

  // ── TenantClusterPool creation modal ──
  const [showPoolCreateModal, setShowPoolCreateModal] = useState(false);
  const [poolCreateCIs, setPoolCreateCIs] = useState<string[]>([]);
  const [poolCreateEnabled, setPoolCreateEnabled] = useState(false);
  const [poolCreateStatusCheck, setPoolCreateStatusCheck] = useState<Array<{
    name: string; exists: boolean; enabled: boolean; available_clusters: number; action_preview: string;
  }> | null>(null);
  const [poolCreateStatusLoading, setPoolCreateStatusLoading] = useState(false);
  const [poolCreateMin, setPoolCreateMin] = useState(0);
  const [poolCreateMax, setPoolCreateMax] = useState(3);
  const [poolCreateMinAvailPlacements, setPoolCreateMinAvailPlacements] = useState(0);
  const [poolCreateMaxPlacements, setPoolCreateMaxPlacements] = useState(15);
  const [poolCreateEnvLevel, setPoolCreateEnvLevel] = useState('integration');
  const [poolCreateCloud, setPoolCreateCloud] = useState('cnv-dedicated-shared');
  const [poolCreateYaml, setPoolCreateYaml] = useState('');
  const [poolCreateResults, setPoolCreateResults] = useState<Array<{ name: string; success: boolean; action: string; output: string; error: string }>>([]);
  const [poolCreateLoading, setPoolCreateLoading] = useState(false);
  const [poolCreateApplied, setPoolCreateApplied] = useState(false);
  const [poolCreateTimeShifted, setPoolCreateTimeShifted] = useState<number>(0);

  // ── Schedule validation warnings ──
  const warnings = useMemo(() => {
    const warns: ScheduleWarning[] = [];
    const now = new Date();

    // Same CI + Namespace on different days is normal for multi-day events.
    // Only flag when 2+ sessions share CI+NS on the same UTC provision day.
    type DayEntry = { index: number; ci_name: string };
    const byCiNsDay = new Map<string, DayEntry[]>();
    schedules.forEach((s, i) => {
      if (s.multi_workshop_name) return;
      const prov = parseScheduleDate(s.provisioning_date);
      if (!prov) return;
      const day = `${prov.getUTCFullYear()}-${String(prov.getUTCMonth() + 1).padStart(2, '0')}-${String(prov.getUTCDate()).padStart(2, '0')}`;
      const key = `${s.ci}||${s.namespace}||${day}`;
      const list = byCiNsDay.get(key) ?? [];
      list.push({ index: i, ci_name: s.ci_name });
      byCiNsDay.set(key, list);
    });
    for (const [key, list] of byCiNsDay) {
      if (list.length < 2) continue;
      const day = key.split('||')[2];
      const rows = list.map((e) => e.index + 1).join(', ');
      warns.push({
        index: list[0].index,
        indices: list.map((e) => e.index),
        field: 'ci',
        kind: 'conflict',
        message: `"${list[0].ci_name}" — ${list.length} sessions on ${day} UTC share the same CI + Namespace (rows ${rows})`,
      });
    }

    schedules.forEach((s, i) => {
      const prov = parseScheduleDate(s.provisioning_date);
      const stop = parseScheduleDate(s.auto_stop);
      const destroy = parseScheduleDate(s.auto_destroy);

      // Unparseable dates
      if (s.provisioning_date && !prov)
        warns.push({ index: i, field: 'provisioning_date', kind: 'other', message: `"${s.ci_name}" has an unparseable provisioning date: "${s.provisioning_date}"` });
      if (s.auto_stop && !stop)
        warns.push({ index: i, field: 'auto_stop', kind: 'other', message: `"${s.ci_name}" has an unparseable auto-stop date: "${s.auto_stop}"` });
      if (s.auto_destroy && !destroy)
        warns.push({ index: i, field: 'auto_destroy', kind: 'other', message: `"${s.ci_name}" has an unparseable auto-destroy date: "${s.auto_destroy}"` });

      // Past provisioning date (dates in CSV are UTC; now is also UTC internally)
      if (prov && prov < now)
        warns.push({ index: i, field: 'provisioning_date', kind: 'other', message: `"${s.ci_name}" provisioning date is in the past — ${s.provisioning_date} UTC has already passed` });

      // Auto-stop before provisioning
      if (prov && stop && stop <= prov)
        warns.push({ index: i, field: 'auto_stop', kind: 'other', message: `"${s.ci_name}" auto-stop is before or equal to provisioning date` });

      // Missing required dates
      if (!s.provisioning_date?.trim())
        warns.push({ index: i, field: 'provisioning_date', kind: 'other', message: `"${s.ci_name}" is missing a provisioning date` });
      if (!s.auto_stop?.trim())
        warns.push({ index: i, field: 'auto_stop', kind: 'other', message: `"${s.ci_name}" is missing an auto-stop date` });
      if (!s.auto_destroy?.trim())
        warns.push({ index: i, field: 'auto_destroy', kind: 'other', message: `"${s.ci_name}" is missing an auto-destroy date` });

      // CI format check (expect vendor.item.env pattern)
      if (s.ci && !s.ci.includes('.'))
        warns.push({ index: i, field: 'ci', kind: 'other', message: `"${s.ci_name}" CI "${s.ci}" may be invalid (expected format: vendor.item.env)` });

      // Namespace format check
      if (s.namespace && !/^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/.test(s.namespace))
        warns.push({ index: i, field: 'namespace', kind: 'other', message: `"${s.ci_name}" namespace "${s.namespace}" may be invalid (must be lowercase alphanumeric with hyphens)` });

      // Users reasonableness
      if (s.users !== null && s.users > 500)
        warns.push({ index: i, field: 'users', kind: 'other', message: `"${s.ci_name}" has a high user count (${s.users}) — verify this is intentional` });
      if (s.users !== null && s.users < 1)
        warns.push({ index: i, field: 'users', kind: 'other', message: `"${s.ci_name}" has an invalid user count (${s.users})` });

      // num_users catalog limit check
      if (s.users !== null && s.ci in numUsersLimits && s.users > numUsersLimits[s.ci])
        warns.push({ index: i, field: 'users', kind: 'other', message: `"${s.ci_name}" exceeds catalog limit: ${s.users} users requested, max ${numUsersLimits[s.ci]}` });

      // Blank optional fields - removed informational warnings as these fields have defaults
    });
    return warns;
  }, [schedules, numUsersLimits]);

  const warningRowIndices = useMemo(
    () => new Set(warnings.flatMap((w) => w.indices ?? [w.index])),
    [warnings],
  );

  const conflictWarnings = useMemo(() => warnings.filter((w) => w.kind === 'conflict'), [warnings]);
  const otherWarnings = useMemo(() => warnings.filter((w) => w.kind !== 'conflict'), [warnings]);
  const [warningsExpanded, setWarningsExpanded] = useState(false);

  const hasMissingDateWarnings = useMemo(() => {
    return warnings.some(w =>
      (w.field === 'provisioning_date' && w.message.includes('missing')) ||
      (w.field === 'auto_stop' && w.message.includes('missing')) ||
      (w.field === 'auto_destroy' && w.message.includes('missing'))
    );
  }, [warnings]);

  // Filtered schedules for preview search
  const filteredSchedules = useMemo(() => {
    if (!previewSearch) return schedules.map((s, i) => ({ s, i }));
    const q = previewSearch.toLowerCase();
    return schedules
      .map((s, i) => ({ s, i }))
      .filter(({ s }) =>
        s.ci_name.toLowerCase().includes(q) ||
        s.ci.toLowerCase().includes(q) ||
        s.namespace.toLowerCase().includes(q) ||
        s.workshop_name.toLowerCase().includes(q)
      );
  }, [schedules, previewSearch]);
  const hasMultiAsset = schedules.some(s => s.is_multi_asset);
  const needsPasswordWarning = hasMultiAsset && passwordCount === null;

  // auto-scroll log
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [logLines]);

  // Pool lookup - fetch pool data when enabled
  useEffect(() => {
    if (!usePoolLookup || schedules.length === 0) {
      setPoolLookupData({});
      setAllPools([]);
      return;
    }

    const fetchPoolData = async () => {
      // Fetch pools for each CI + all pools for override selection
      const uniqueCIs = Array.from(new Set(schedules.map(s => s.ci)));
      const [poolResults, allPoolsRes] = await Promise.all([
        Promise.all(uniqueCIs.map(ci =>
          api.lookupPool(ci).catch(() => ({ catalog_item: ci, pool: null, has_pool: false }))
        )),
        api.listAllPools().catch(() => ({ pools: [] }))
      ]);

      const poolMap: Record<string, import('../types').PoolLookupResponse> = {};
      poolResults.forEach(r => {
        poolMap[r.catalog_item] = r;
      });
      setPoolLookupData(poolMap);
      setAllPools(allPoolsRes.pools || []);
    };

    fetchPoolData();
  }, [usePoolLookup, schedules, targetCluster]);

  // Re-run tenant cluster checks when the deploy target changes — pools differ per cluster
  useEffect(() => {
    if (schedules.length === 0) return;
    api.checkTenantClusterRefs(targetCluster).then(setMissingTenantRefs).catch(() => {});
    api.getClusterNeeds(targetCluster).then(setClusterNeeds).catch(() => {});
  }, [targetCluster]); // eslint-disable-line react-hooks/exhaustive-deps

  /** Re-fetch namespace + catalog num_users + catalog namespace + pool capacity checks from the server (uses loaded schedules). */
  const refreshClusterValidation = useCallback(async () => {
    setMissingNamespaces([]);
    setNumUsersViolations([]);
    setUsersNotInCatalog([]);
    setUsersBlankCatalogDefault([]);
    setNumUsersLimits({});
    setParamViolations([]);
    setParamWarnings([]);
    setCatalogNamespaceMismatches([]);
    setCatalogNotFound([]);
    setCatalogSuffixCorrections([]);
    setProdNotEvent([]);
    setSkippedCatalogSummary(null);
    setPoolCapacityWarnings([]);
    setPoolsNotFound([]);
    const [nsRes, nuRes, cnRes, pcRes, pvRes] = await Promise.all([
      api.validateNamespaces(),
      api.validateNumUsers(),
      api.validateCatalogNamespaces(),
      api.validatePoolCapacity(),
      api.validateParameters(),
    ]);
    if (nsRes.missing.length) setMissingNamespaces(nsRes.missing);
    if (nuRes.violations.length) setNumUsersViolations(nuRes.violations);
    if (nuRes.users_not_in_catalog?.length) setUsersNotInCatalog(nuRes.users_not_in_catalog);
    if (nuRes.users_blank_catalog_default?.length) setUsersBlankCatalogDefault(nuRes.users_blank_catalog_default);
    if (Object.keys(nuRes.limits).length) setNumUsersLimits(nuRes.limits);
    if (pvRes.violations?.length) setParamViolations(pvRes.violations);
    if (pvRes.warnings?.length) setParamWarnings(pvRes.warnings);
    if (cnRes.mismatches.length) {
      setCatalogNamespaceMismatches(cnRes.mismatches);
      // Reflect the corrected namespace in the schedule table so users see where deploy will go
      try {
        const current = await api.getSchedules();
        const correctedNs = new Map<string, string>(
          cnRes.mismatches.map((m: import('../types').CatalogNamespaceMismatch) => [m.ci, m.found_catalog_namespace])
        );
        setSchedules(current.map(s => correctedNs.has(s.ci) ? { ...s, catalog_namespace: correctedNs.get(s.ci)! } : s));
      } catch {
        // Non-fatal — table may still show original namespace but deploy will redirect correctly
      }
    }
    // Auto-correct bare CI → suggested suffix (single hit, or .prod when no .event) — same pattern as namespace.
    const autoSuffix = (cnRes.not_found || []).filter((n) => n.suggested_ci && n.suggested_ci !== n.ci);
    const stillMissing = (cnRes.not_found || []).filter((n) => !n.suggested_ci);
    if (autoSuffix.length > 0) {
      const mapping = new Map(autoSuffix.map((n) => [n.ci, n.suggested_ci as string]));
      try {
        const current = await api.getSchedules();
        const updated = current.map((s) => {
          let next = s;
          const main = mapping.get(s.ci);
          if (main) {
            const suffix = main.endsWith('.event') ? 'event' : main.endsWith('.dev') ? 'dev' : 'prod';
            next = {
              ...next,
              ci: main,
              catalog_namespace:
                suffix === 'event' ? 'babylon-catalog-event'
                  : suffix === 'dev' ? 'babylon-catalog-dev'
                    : 'babylon-catalog-prod',
            };
          }
          if (next.asset_cis) {
            const assets = next.asset_cis.split(',').map((a) => a.trim()).filter(Boolean);
            const rewritten = assets.map((a) => mapping.get(a) || a);
            if (rewritten.some((a, i) => a !== assets[i])) {
              next = { ...next, asset_cis: rewritten.join(',') };
            }
          }
          return next;
        });
        setSchedules(updated);
        await api.updateSchedules(updated);
        setCatalogSuffixCorrections(autoSuffix.map((n) => ({
          ci: n.ci,
          corrected_ci: n.suggested_ci as string,
          ci_name: n.ci_name,
          message: n.message,
        })));
        await api.addOperatorOverride({
          action: 'auto_ci_suffix_correct',
          summary: `Auto-corrected ${autoSuffix.length} bare CI(s) to env suffix (diverges from Labagator)`,
          detail: autoSuffix.map((n) => `${n.ci} → ${n.suggested_ci}`).join('; '),
          affected_count: autoSuffix.length,
          source: 'upload',
        }).catch(() => {});
        onOperatorOverrideRecorded?.();
      } catch {
        setCatalogNotFound(cnRes.not_found || []);
        setCatalogSuffixCorrections([]);
      }
      setCatalogNotFound(stillMissing);
    } else if (cnRes.not_found.length) {
      setCatalogNotFound(cnRes.not_found);
      setCatalogSuffixCorrections([]);
    }
    if (pcRes.warnings?.length) setPoolCapacityWarnings(pcRes.warnings);
    if (pcRes.not_found?.length) setPoolsNotFound(pcRes.not_found);
    if (cnRes.prod_not_event?.length) setProdNotEvent(cnRes.prod_not_event);
    else setProdNotEvent([]);
    return { nsRes, nuRes, cnRes, pcRes, pvRes };
  }, [onOperatorOverrideRecorded]);

  const handleValidate = async () => {
    if (schedules.length === 0) {
      showToast('Upload a CSV first', 'danger');
      return;
    }
    setValidating(true);
    try {
      const { nsRes, nuRes, cnRes, pcRes, pvRes } = await refreshClusterValidation();
      const refs = await api.checkTenantClusterRefs(targetCluster);
      setMissingTenantRefs(refs);
      // Namespace mismatches + bare→.prod (no .event) are auto-corrected — only real ambiguity / pool issues need operator action.
      const stillNeedCiPick = (cnRes.not_found || []).filter((n) => !n.suggested_ci).length;
      if (stillNeedCiPick > 0 || pcRes.not_found.length || pcRes.warnings.length) {
        showToast('Prerequisite checks found issues on the selected target. Review the alerts below.', 'danger');
        return;
      }
      const nNs = nsRes.missing.length;
      const nNu = nuRes.violations.length;
      const nAdv = nuRes.users_not_in_catalog?.length ?? 0;
      const nPv = pvRes.violations?.length ?? 0;
      if (nNs === 0 && nNu === 0 && nAdv === 0 && nPv === 0) {
        showToast(
          'Validation passed: namespaces found on cluster; num_users and catalog parameters within limits where checked.',
          'success',
        );
      } else {
        showToast(
          `Validation: ${nNs} missing namespace(s), ${nNu} num_users over limit, ${nPv} invalid parameter(s), ${nAdv} catalog/Users mismatch — see alerts below.`,
          'info',
        );
      }
    } catch (e) {
      showToast(`Validation failed: ${e}`, 'danger');
    } finally {
      setValidating(false);
    }
  };

  const handleDownloadYaml = async () => {
    if (schedules.length === 0) {
      showToast('Upload a CSV first', 'danger');
      return;
    }

    setYamlDownloading(true);
    try {
      await api.downloadDryRunYaml({
        dry_run: true,
        resource_lock: resourceLock,
        enable_resource_pools: enableResourcePools,
        white_glove: whiteGlove,
        redirect,
        showroom_novnc: showroomNovnc,
        showroom_zerotouch: showroomZerotouch,
        target_cluster: targetCluster || null,
      });
      showToast('Downloaded dry-run manifest YAML', 'success');
    } catch (e) {
      showToast(`YAML download failed: ${e}`, 'danger');
    } finally {
      setYamlDownloading(false);
    }
  };

  const toggleExpanded = (idx: number) => {
    setExpandedRows(prev => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx); else next.add(idx);
      return next;
    });
  };

  const handleCatalogOverride = (action: 'event' | 'prod' | 'dev' | 'clear') => {
    setCatalogOverrideAction(action);
    setShowCatalogOverrideModal(true);
  };

  const confirmCatalogOverride = async () => {
    if (!catalogOverrideAction) return;

    const newValue = catalogOverrideAction === 'clear' ? '' :
                     catalogOverrideAction === 'event' ? 'babylon-catalog-event' :
                     catalogOverrideAction === 'prod' ? 'babylon-catalog-prod' :
                     'babylon-catalog-dev';

    const updated = schedules.map(s => ({ ...s, catalog_namespace: newValue }));
    setSchedules(updated);

    try {
      await api.updateSchedules(updated);
      const msg = catalogOverrideAction === 'clear'
        ? `Cleared catalog namespace override for ${schedules.length} workshop(s) - using auto-detection`
        : `Set catalog namespace to ${newValue} for ${schedules.length} workshop(s)`;
      showToast(msg, 'success');
    } catch (err) {
      showToast(`Failed to update schedules: ${err}`, 'danger');
    }

    setShowCatalogOverrideModal(false);
    setCatalogOverrideAction(null);
  };

  const handleFillMissingDates = async () => {
    if (!fillProvDate || !fillStopDate || !fillDestroyDate) {
      showToast('Please fill in all three dates', 'danger');
      return;
    }

    try {
      const result = await api.fillMissingDates({
        provisioning_date: fillProvDate,
        auto_stop: fillStopDate,
        auto_destroy: fillDestroyDate,
      });
      // Refresh schedules from backend
      const updated = await api.getSchedules();
      setSchedules(updated);
      showToast(result.message, 'success');
      setShowFillDatesModal(false);
      setFillProvDate('');
      setFillStopDate('');
      setFillDestroyDate('');
    } catch (err) {
      showToast(`Failed to fill missing dates: ${err}`, 'danger');
    }
  };

  const handleUpload = async () => {
    if (!csvFile) { showToast('Please select a CSV file', 'danger'); return; }
    try {
      const data = importMode === 'labagator'
        ? await api.importLabagatorCSV(csvFile, {
            default_ci: labagatorDefaultCI || undefined,
            default_users: labagatorDefaultUsers,
            default_redirect: redirect,
            default_white_glove: whiteGlove,
            buffer_hours: labagatorBufferHours,
          })
        : await api.uploadCSV(csvFile);

      // Apply global redirect setting to uploaded schedules
      const schedulesWithRedirect = data.schedules.map(s => ({ ...s, redirect }));
      setSchedules(schedulesWithRedirect);

      // Update backend with redirect setting
      try {
        await api.updateSchedules(schedulesWithRedirect);
      } catch (err) {
        console.warn('Failed to apply redirect setting to backend:', err);
      }

      setSkippedRows(data.skipped_rows ?? 0);
      setTotalRows(data.total_rows ?? 0);

      const msg = importMode === 'labagator'
        ? data.skipped_rows
          ? `Imported ${data.count} of ${data.total_rows} session(s) — ${data.skipped_rows} skipped`
          : `Imported ${data.count} Labagator session(s)`
        : data.skipped_rows
          ? `Loaded ${data.count} of ${data.total_rows} row(s) — ${data.skipped_rows} row(s) skipped`
          : `Loaded ${data.count} schedule(s)`;

      showToast(msg, data.skipped_rows ? 'danger' : 'success');
      try {
        await refreshClusterValidation();

        // Check cluster capacity needs
        try {
          const needsRes = await api.getClusterNeeds(targetCluster);
          setClusterNeeds(needsRes);
        } catch (e) {
          console.warn('Cluster needs check failed', e);
        }

        // Check tenant cluster references
        try {
          const refsRes = await api.checkTenantClusterRefs();
          setMissingTenantRefs(refsRes);
        } catch (e) {
          console.warn('Tenant cluster reference check failed', e);
        }

      } catch (e) {
        console.warn('Post-upload cluster validation failed', e);
      }
    } catch (e) {
      showToast(`${importMode === 'labagator' ? 'Import' : 'Upload'} failed: ${e}`, 'danger');
    }
  };

  const NAMESPACE_RE = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/;

  // Reset the session picker whenever the chosen event changes — the previously
  // fetched sessions belong to a different event.
  useEffect(() => {
    setLabagatorSessions([]);
    setLabagatorSessionsFetched(false);
    setSelectedSessionIds(new Set());
  }, [selectedEventId]);

  const handleFetchLabagatorSessions = async () => {
    if (selectedEventId === null) { showToast('Please select an event', 'danger'); return; }
    const event = labagatorEvents.find((e) => e.id === selectedEventId);
    setLabagatorSessionsLoading(true);
    try {
      const res = await api.listLabagatorSessions({ event_id: selectedEventId, event_name: event?.name });
      setLabagatorSessions(res.sessions);
      setLabagatorSessionsFetched(true);
      // Default to all selected — the common case is "deploy everything".
      setSelectedSessionIds(new Set(res.sessions.map((s) => s.room_session_id)));
      if (res.sessions.length === 0) {
        showToast('No deployable sessions found for this event.', 'info');
      }
    } catch (e) {
      showToast(`Fetch sessions failed: ${e}`, 'danger');
    } finally {
      setLabagatorSessionsLoading(false);
    }
  };

  const toggleSession = (id: number, checked: boolean) => {
    setSelectedSessionIds((prev) => {
      const next = new Set(prev);
      if (checked) next.add(id); else next.delete(id);
      return next;
    });
  };

  const allSessionsSelected =
    labagatorSessions.length > 0 && selectedSessionIds.size === labagatorSessions.length;

  const toggleSelectAllSessions = (checked: boolean) => {
    setSelectedSessionIds(checked ? new Set(labagatorSessions.map((s) => s.room_session_id)) : new Set());
  };

  const handleLabagatorPreview = async () => {
    if (labagatorPreviewing) return;
    if (selectedEventId === null) { showToast('Please select an event', 'danger'); return; }
    if (!labagatorNamespace || labagatorNamespace.length > 63 || !NAMESPACE_RE.test(labagatorNamespace)) {
      setLabagatorNamespaceError('Invalid namespace: must match [a-z0-9-], 1-63 chars');
      return;
    }
    setLabagatorNamespaceError('');
    const event = labagatorEvents.find((e) => e.id === selectedEventId);
    if (!event) { showToast('Selected event not found', 'danger'); return; }

    // When the session picker has been used, import only the checked sessions.
    // With no fetch (picker untouched), import the whole event as before.
    let roomSessionIds: number[] | undefined;
    if (labagatorSessionsFetched) {
      if (selectedSessionIds.size === 0) {
        showToast('Select at least one session to import', 'danger');
        return;
      }
      roomSessionIds = Array.from(selectedSessionIds);
    }

    setLabagatorPreviewing(true);
    try {
      const preview = await api.previewLabagatorImport({
        event_id: selectedEventId,
        namespace: labagatorNamespace,
        event_name: event.name,
        enable_workshop_interface: labagatorEnableWorkshopInterface,
        concurrency: labagatorConcurrency,
        white_glove: whiteGlove,
        auto_stop_days: labagatorAutoStopDays,
        auto_destroy_days: labagatorAutoDestroyDays,
        room_session_ids: roomSessionIds,
      });
      if (preview.session_count === 0) {
        showToast('No sessions found for the current selection.', 'info');
        return;
      }
      setLabagatorPreview(preview);
      setShowLabagatorConfirm(true);
    } catch (e) {
      showToast(`Preview failed: ${e}`, 'danger');
    } finally {
      setLabagatorPreviewing(false);
    }
  };

  const handleLabagatorConfirm = async () => {
    if (!labagatorPreview || labagatorImporting) return;
    setLabagatorImporting(true);
    setShowLabagatorConfirm(false);
    try {
      const data = await api.importFromLabagator(labagatorPreview.csv_text, `${labagatorPreview.event_name}.csv`);
      setSchedules(data.schedules);
      setSkippedRows(data.skipped_rows ?? 0);
      setTotalRows(data.total_rows ?? 0);
      showToast(`Imported ${data.count} session(s) from ${labagatorPreview.event_name}`, 'success');
      try {
        await refreshClusterValidation();
      } catch (e) {
        console.warn('Post-import cluster validation failed', e);
      }
    } catch (e) {
      showToast(`Import failed: ${e}`, 'danger');
    } finally {
      setLabagatorPreview(null);
      setLabagatorImporting(false);
    }
  };

  const handleLoadExample = async (slug: string) => {
    setLoadingExampleSlug(slug);
    try {
      const data = await api.loadScheduleExample(slug);
      // Apply global redirect setting to example schedules
      const schedulesWithRedirect = data.schedules.map(s => ({ ...s, redirect }));
      setSchedules(schedulesWithRedirect);

      // Update backend with redirect setting
      try {
        await api.updateSchedules(schedulesWithRedirect);
      } catch (err) {
        console.warn('Failed to apply redirect setting to backend:', err);
      }

      setSkippedRows(data.skipped_rows ?? 0);
      setTotalRows(data.total_rows ?? 0);
      const msg = data.skipped_rows
        ? `Loaded example ${data.count} of ${data.total_rows} row(s) — ${data.skipped_rows} skipped`
        : `Loaded example: ${data.count} schedule(s)`;
      showToast(msg, data.skipped_rows ? 'danger' : 'success');
      try {
        await refreshClusterValidation();
      } catch (e) {
        console.warn('Post-example cluster validation failed', e);
      }
    } catch (e) {
      showToast(`Example load failed: ${e}`, 'danger');
    } finally {
      setLoadingExampleSlug(null);
    }
  };

  const handleUploadPasswords = async () => {
    if (!passwordFile) { showToast('Please select a passwords CSV file', 'danger'); return; }
    try {
      const data = await api.uploadPasswordsCSV(passwordFile);
      setPasswordCount(data.count);
      showToast(data.message, 'success');
    } catch (e) {
      showToast(`Password upload failed: ${e}`, 'danger');
    }
  };

  const handleClear = async () => {
    setShowClearConfirm(false);
    try {
      await api.clearSession();
      onClear();
      setLogLines([]);
      setProgress(0);
      setProgressMsg('');
      setPasswordCount(null);
      setCsvFile(null);
      setCsvFilename('');
      setPasswordFile(null);
      setPasswordFilename('');
      setExpandedRows(new Set());
      setSkippedRows(0);
      setTotalRows(0);
      setResourceLock(true);
      setEnableResourcePools(false);
      setWhiteGlove(true);
      setRedirect(true);
      setShowroomNovnc(false);
      setShowroomZerotouch(false);
      setNumUsersViolations([]);
      setNumUsersLimits({});
      showToast('Session cleared', 'success');
    } catch (e) {
      showToast(`Clear failed: ${e}`, 'danger');
    }
  };

  const handleDryRun = async () => {
    if (schedules.length === 0) { showToast('Upload a CSV first', 'danger'); return; }
    try {
      const data = await api.dryRun({ dry_run: true, resource_lock: resourceLock, enable_resource_pools: enableResourcePools, white_glove: whiteGlove, redirect, showroom_novnc: showroomNovnc, showroom_zerotouch: showroomZerotouch, target_cluster: targetCluster || null });
      setResults(data);
      showToast(`Dry-run: ${data.length} result(s)`, 'success');
    } catch (e) {
      showToast(`Dry-run failed: ${e}`, 'danger');
    }
  };

  const appendLog = useCallback((line: string) => {
    setLogLines(prev => [...prev, line]);
  }, []);

  const handleDeploy = async () => {
    if (!dryRun && !showDeployConfirm) {
      setDeployRiskAck(false);
      setShowDeployConfirm(true);
      return;
    }
    setShowDeployConfirm(false);

    if (schedules.length === 0) { showToast('Upload a CSV first', 'danger'); return; }
    // Blocking issues are now shown in confirmation modal with disabled deploy button
    setDeploying(true);
    setDeployPaused(false);
    setProgress(0);
    setProgressMsg('Starting...');
    setLogLines([]);

    try {
      try {
        const overrides = await api.getOperatorOverrides();
        if (overrides?.length) {
          appendLog(`OPERATOR OVERRIDES: ${overrides.length} local tweak(s) — Labagator was not the sole source of truth`);
          for (const o of overrides) {
            appendLog(`  [${o.action}] ${o.summary}${o.affected_count ? ` (${o.affected_count} row(s))` : ''}`);
          }
        }
      } catch { /* non-fatal */ }
      const job = await api.deploy({ dry_run: dryRun, resource_lock: resourceLock, enable_resource_pools: enableResourcePools, white_glove: whiteGlove, redirect, showroom_novnc: showroomNovnc, showroom_zerotouch: showroomZerotouch, deploy_delay_seconds: deployDelaySeconds === '' ? null : Number(deployDelaySeconds), target_cluster: targetCluster || null });
      jobIdRef.current = job.job_id;
      const ws = api.deployWebSocket(job.job_id);
      wsRef.current = ws;

      const handleStatus = (d: Record<string, unknown>) => {
        if (d.keepalive) return;
        setProgress(d.progress as number);
        setProgressMsg((d.message as string) || '');
        if (d.message) appendLog(d.message as string);
        if (d.status === 'paused') setDeployPaused(true);
        if (d.status === 'running') setDeployPaused(false);

        if (d.status === 'completed' || d.status === 'failed' || d.status === 'cancelled') {
          ws.close();
          wsRef.current = null;
          jobIdRef.current = null;
          if (pollRef.current) {
            clearInterval(pollRef.current);
            pollRef.current = null;
          }
          setDeploying(false);
          setDeployPaused(false);
          if (d.log_file) setDeployLogFile?.(d.log_file as string);
          if (d.status === 'completed') {
            showToast('Deployment completed', 'success');
            api.deployResults().then(r => setResults(r)).catch((err) => { console.warn('Failed to fetch results', err); });
          } else if (d.status === 'cancelled') {
            showToast(`Deployment cancelled after ${d.progress}%`, 'info');
            api.deployResults().then(r => setResults(r)).catch(() => {});
          } else {
            showToast(`Deployment failed: ${d.error || 'unknown'}`, 'danger');
          }
        }
      };

      ws.onmessage = (e) => {
        try { handleStatus(JSON.parse(e.data)); } catch { /* ignore parse errors */ }
      };
      ws.onerror = () => {
        appendLog('WebSocket error — falling back to polling');
        ws.close();
        wsRef.current = null;
        if (pollRef.current) clearInterval(pollRef.current);
        pollRef.current = setInterval(async () => {
          try {
            const s = await api.deployStatus(job.job_id);
            handleStatus(s as unknown as Record<string, unknown>);
            if (s.status === 'completed' || s.status === 'failed' || s.status === 'cancelled') {
              if (pollRef.current) {
                clearInterval(pollRef.current);
                pollRef.current = null;
              }
            }
          } catch {
            if (pollRef.current) {
              clearInterval(pollRef.current);
              pollRef.current = null;
            }
            setDeploying(false);
          }
        }, 2000);
      };
    } catch (e) {
      setDeploying(false);
      showToast(`Deploy failed: ${e}`, 'danger');
    }
  };

  const handleDeployCancel = () => {
    if (jobIdRef.current) {
      try {
        wsRef.current?.send(JSON.stringify({ command: 'cancel' }));
      } catch (e) {
        console.warn('WebSocket cancel failed', e);
      }
      api.deployCancel(jobIdRef.current).catch((e) => {
        console.warn('HTTP cancel failed', e);
        showToast(`Cancel request failed: ${e}`, 'danger');
      });
      appendLog('Cancel requested...');
    }
  };

  const handleDeployPause = () => {
    if (jobIdRef.current) {
      if (deployPaused) {
        try {
          wsRef.current?.send(JSON.stringify({ command: 'resume' }));
        } catch (e) {
          console.warn('WebSocket resume failed', e);
        }
        api.deployResume(jobIdRef.current).catch((e) => {
          console.warn('HTTP resume failed', e);
          showToast(`Resume request failed: ${e}`, 'danger');
        });
        appendLog('Resuming...');
      } else {
        try {
          wsRef.current?.send(JSON.stringify({ command: 'pause' }));
        } catch (e) {
          console.warn('WebSocket pause failed', e);
        }
        api.deployPause(jobIdRef.current).catch((e) => {
          console.warn('HTTP pause failed', e);
          showToast(`Pause request failed: ${e}`, 'danger');
        });
        appendLog('Pausing after current workshop...');
      }
    }
  };

  const columnCount = 14; // Updated for Item Type + Cluster Link columns

  // Block deploy only for hard failures (num_users, direct-sandbox with no
  // clusters). Missing catalog items are a warning — skip them to deploy the rest.
  const _tenantBlockDirect = missingTenantRefs
    ? [...(missingTenantRefs.missing_refs || []), ...(missingTenantRefs.ref_no_pool || [])]
        .filter((r: any) => !r.pool_exists && !r.has_cluster_row && r.direct_sandbox)
    : [];
  const catalogNotFoundUnique = uniqueByCi(catalogNotFound);
  const catalogSuffixOpts = (n: import('../types').CatalogNotFoundItem) =>
    (n.suffix_options && n.suffix_options.length > 0)
      ? n.suffix_options
      : (n.suggested_ci ? [n.suggested_ci] : []);
  /** Remaining not-found that still have a published .prod (usually also .event → must pick). */
  const catalogRemainingWithProd = catalogNotFoundUnique.filter((n) =>
    catalogSuffixOpts(n).some((o) => o.endsWith('.prod')),
  );
  const catalogHasEventOpt = catalogNotFoundUnique.filter((n) =>
    catalogSuffixOpts(n).some((o) => o.endsWith('.event')),
  );
  const highUsersIssues = usersNotInCatalog.filter((a) => a.severity === 'high');
  const hasDeployRisks =
    catalogNotFoundUnique.length > 0 ||
    (skippedCatalogSummary?.items.length ?? 0) > 0 ||
    highUsersIssues.length > 0;
  const hasBlockingIssues =
    numUsersViolations.length > 0 ||
    paramViolations.length > 0 ||
    _tenantBlockDirect.length > 0;
  const deployBlocked = hasBlockingIssues;

  const requestSkipMissing = () => {
    if (catalogNotFoundUnique.length === 0) return;
    setRiskConfirm({
      kind: 'skip-missing',
      title: 'Skip missing catalog items?',
      body: `Remove ${catalogNotFoundUnique.length} unique CI(s) from this Flow session so the rest can deploy. Labagator is not changed.`,
      bullets: catalogNotFoundUnique.slice(0, 8).map((n) => `${n.ci_name} — ${n.ci}`),
    });
  };

  const requestApplyEventWhereAvailable = () => {
    if (catalogHasEventOpt.length === 0) return;
    setRiskConfirm({
      kind: 'apply-event',
      title: 'Use .event where published?',
      body:
        `Rewrite ${catalogHasEventOpt.length} bare CI(s) to .event. ` +
        `Only rows that have a published .event item are changed. Not Labagator.`,
      bullets: catalogHasEventOpt.slice(0, 8).map((n) => `${n.ci} → ${n.ci}.event`),
    });
  };

  const requestUsersFix = (mode: 'move-to-instances' | 'clear-users') => {
    if (usersNotInCatalog.length === 0) return;
    setRiskConfirm({
      kind: mode === 'move-to-instances' ? 'users-to-instances' : 'clear-users',
      title: mode === 'move-to-instances' ? 'Move Users → Instances?' : 'Clear Users (keep Instances)?',
      body:
        mode === 'move-to-instances'
          ? `Copy Users into Instances and clear Users on ${usersNotInCatalog.length} advisory row(s). Local Flow only — not Labagator.`
          : `Clear Users and leave Instances as-is on ${usersNotInCatalog.length} advisory row(s). Local Flow only — not Labagator.`,
      bullets: usersNotInCatalog.slice(0, 6).map((a) => `${a.ci_name} (Users=${a.users})`),
    });
  };

  const requestCapUsers = () => {
    if (numUsersViolations.length === 0) return;
    setRiskConfirm({
      kind: 'cap-users',
      title: 'Cap Users to catalog max?',
      body: `Lower Users on ${numUsersViolations.length} row(s) to the catalog maximum so deploy is unblocked. Local Flow only — not Labagator.`,
      bullets: numUsersViolations.slice(0, 6).map((v) => `${v.ci_name}: ${v.requested_users} → ${v.maximum}`),
    });
  };

  const confirmRiskAction = async () => {
    const kind = riskConfirm?.kind;
    setRiskConfirm(null);
    if (!kind) return;
    if (kind === 'skip-missing') await skipMissingCatalogItems();
    else if (kind === 'apply-prod-no-event') await applySuffixChoice('prod', catalogRemainingWithProd);
    else if (kind === 'apply-event') await applySuffixChoice('event', catalogHasEventOpt);
    else if (kind === 'users-to-instances') await applyUsersNotInCatalogFix('move-to-instances');
    else if (kind === 'clear-users') await applyUsersNotInCatalogFix('clear-users');
    else if (kind === 'cap-users') await capUsersToCatalogMax();
  };

  /** Persist an operator-accepted local tweak so Deployments/logs show it was not Labagator. */
  const recordOperatorOverride = useCallback(async (body: {
    action: string;
    summary: string;
    detail?: string;
    affected_count?: number;
  }) => {
    try {
      await api.addOperatorOverride({ ...body, source: 'upload' });
      onOperatorOverrideRecorded?.();
    } catch (e) {
      console.warn('Failed to record operator override', e);
    }
  }, [onOperatorOverrideRecorded]);

  const skipMissingCatalogItems = async () => {
    const missing = new Set(catalogNotFound.map((n) => n.ci));
    if (missing.size === 0) return;

    const byCi = new Map(catalogNotFoundUnique.map((n) => [n.ci, n]));
    const removedRows: typeof schedules = [];
    const kept = schedules.filter((s) => {
      if (missing.has(s.ci)) {
        removedRows.push(s);
        return false;
      }
      if (s.asset_cis) {
        const assets = s.asset_cis.split(',').map((a) => a.trim()).filter(Boolean);
        if (assets.some((a) => missing.has(a))) {
          removedRows.push(s);
          return false;
        }
      }
      return true;
    });

    // One summary line per missing CI (workshop + why)
    const itemSummary = [...byCi.values()].map((nf) => {
      const rowsForCi = removedRows.filter(
        (s) =>
          s.ci === nf.ci ||
          (s.asset_cis || '')
            .split(',')
            .map((a) => a.trim())
            .includes(nf.ci),
      );
      const reason = nf.message.includes('Did you mean')
        ? nf.message.replace(/^Item '[^']+' not found in [^.]+\.\s*/, '')
        : 'not published on this cluster';
      return {
        ci: nf.ci,
        ci_name: nf.ci_name || rowsForCi[0]?.ci_name || nf.ci,
        reason,
      };
    });

    setSchedules(kept);
    setCatalogNotFound([]);
    setSkippedCatalogSummary({
      rows: removedRows.length,
      remaining: kept.length,
      items: itemSummary,
      removedSchedules: removedRows,
    });
    try { await api.updateSchedules(kept); } catch { /* non-fatal */ }
    await recordOperatorOverride({
      action: 'skip_missing_catalog_items',
      summary: `Skipped ${itemSummary.length} missing CI(s) / ${removedRows.length} row(s)`,
      detail: itemSummary.map((i) => `${i.ci_name} (${i.ci})`).join('; '),
      affected_count: removedRows.length,
    });
    showToast(
      removedRows.length > 0
        ? `Skipped ${itemSummary.length} missing CI(s) / ${removedRows.length} row(s) — ${kept.length} remain`
        : 'No schedule rows removed',
      removedRows.length > 0 ? 'info' : 'danger',
    );
  };

  /**
   * Rewrite bare CI → chosen env-suffixed name. Operator-confirmed only.
   */
  const applyCiSuffixMapping = async (
    mapping: Map<string, string>,
    action: string,
    summary: string,
  ) => {
    if (mapping.size === 0) {
      showToast('Nothing to apply', 'danger');
      return;
    }
    let n = 0;
    const updated = schedules.map((s) => {
      let next = s;
      const main = mapping.get(s.ci);
      if (main) {
        n += 1;
        const suffix = main.endsWith('.event') ? 'event' : main.endsWith('.dev') ? 'dev' : 'prod';
        next = {
          ...next,
          ci: main,
          catalog_namespace:
            suffix === 'event' ? 'babylon-catalog-event'
              : suffix === 'dev' ? 'babylon-catalog-dev'
                : 'babylon-catalog-prod',
        };
      }
      if (next.asset_cis) {
        const assets = next.asset_cis.split(',').map((a) => a.trim()).filter(Boolean);
        let changed = false;
        const rewritten = assets.map((a) => {
          const alt = mapping.get(a);
          if (alt) { changed = true; return alt; }
          return a;
        });
        if (changed) {
          if (!main) n += 1;
          next = { ...next, asset_cis: rewritten.join(',') };
        }
      }
      return next;
    });
    setSchedules(updated);
    setCatalogNotFound([]);
    try { await api.updateSchedules(updated); } catch { /* non-fatal */ }
    await recordOperatorOverride({
      action,
      summary,
      detail: [...mapping.entries()].map(([from, to]) => `${from} → ${to}`).join('; '),
      affected_count: n,
    });
    try {
      const cnRes = await api.validateCatalogNamespaces();
      setCatalogNamespaceMismatches(cnRes.mismatches || []);
      setCatalogNotFound(cnRes.not_found || []);
    } catch { /* non-fatal */ }
    showToast(`${summary} (local — not Labagator)`, 'info');
  };

  /** Apply .prod / .event / .dev on remaining not-found rows that have that suffix published. */
  const applySuffixChoice = async (
    suffix: 'prod' | 'event' | 'dev',
    items: import('../types').CatalogNotFoundItem[],
  ) => {
    const mapping = new Map<string, string>();
    for (const n of items) {
      const opts = catalogSuffixOpts(n);
      const hit = opts.find((o) => o.endsWith(`.${suffix}`));
      if (hit) mapping.set(n.ci, hit);
    }
    await applyCiSuffixMapping(
      mapping,
      `apply_catalog_${suffix}_suffix`,
      `Applied .${suffix} on ${mapping.size} CI(s)`,
    );
  };

  /** Match schedule rows to a Users/num_users advisory (main CI or multi-asset child). */
  const scheduleMatchesUsersAdvisory = (s: WorkshopSchedule, a: UsersNotInCatalogAdvisory) => {
    if (s.ci_name !== a.ci_name || s.namespace !== a.namespace) return false;
    if (s.ci === a.ci) return true;
    if (s.asset_cis) {
      return s.asset_cis.split(',').map((x) => x.trim()).includes(a.ci);
    }
    return false;
  };

  /**
   * Local schedule tweaks for testing — Labagator master stays source of truth for the event;
   * these only adjust the in-Flow table so one mismatch does not block a dry-run / partial deploy.
   */
  const applyUsersNotInCatalogFix = async (mode: 'move-to-instances' | 'clear-users') => {
    if (usersNotInCatalog.length === 0) return;
    const touched = new Set<string>();
    const updated = schedules.map((s) => {
      const hits = usersNotInCatalog.filter((a) => scheduleMatchesUsersAdvisory(s, a));
      if (hits.length === 0) return s;
      if (s.users == null || s.users <= 0) return s;
      const key = `${s.ci_name}|${s.namespace}|${s.ci}`;
      touched.add(key);
      if (mode === 'move-to-instances') {
        return { ...s, instances: s.users, users: null };
      }
      return { ...s, users: null };
    });
    if (touched.size === 0) {
      showToast('No matching schedule rows to update', 'danger');
      return;
    }
    setSchedules(updated);
    setUsersNotInCatalog([]);
    try { await api.updateSchedules(updated); } catch { /* non-fatal */ }
    // Re-check so remaining real violations stay visible
    try {
      const nuRes = await api.validateNumUsers();
      setNumUsersViolations(nuRes.violations || []);
      setUsersNotInCatalog(nuRes.users_not_in_catalog || []);
      setUsersBlankCatalogDefault(nuRes.users_blank_catalog_default || []);
      if (Object.keys(nuRes.limits || {}).length) setNumUsersLimits(nuRes.limits);
    } catch { /* non-fatal */ }
    await recordOperatorOverride({
      action: mode === 'move-to-instances' ? 'users_to_instances' : 'clear_users',
      summary:
        mode === 'move-to-instances'
          ? `Moved Users → Instances on ${touched.size} row(s)`
          : `Cleared Users on ${touched.size} row(s) (Instances unchanged)`,
      detail: [...touched].join('; '),
      affected_count: touched.size,
    });
    showToast(
      mode === 'move-to-instances'
        ? `Moved Users → Instances on ${touched.size} row(s) (local only — not Labagator)`
        : `Cleared Users on ${touched.size} row(s); Instances unchanged (local only — not Labagator)`,
      'info',
    );
  };

  const capUsersToCatalogMax = async () => {
    if (numUsersViolations.length === 0) return;
    const byKey = new Map(
      numUsersViolations.map((v) => [`${v.ci_name}|${v.namespace}|${v.ci}`, v]),
    );
    let n = 0;
    const updated = schedules.map((s) => {
      const direct = byKey.get(`${s.ci_name}|${s.namespace}|${s.ci}`);
      let v = direct;
      if (!v && s.asset_cis) {
        for (const asset of s.asset_cis.split(',').map((a) => a.trim()).filter(Boolean)) {
          const hit = byKey.get(`${s.ci_name}|${s.namespace}|${asset}`);
          if (hit) { v = hit; break; }
        }
      }
      if (!v || s.users == null || s.users <= v.maximum) return s;
      n += 1;
      return { ...s, users: v.maximum };
    });
    if (n === 0) {
      showToast('No rows needed capping', 'danger');
      return;
    }
    setSchedules(updated);
    setNumUsersViolations([]);
    try { await api.updateSchedules(updated); } catch { /* non-fatal */ }
    try {
      const nuRes = await api.validateNumUsers();
      setNumUsersViolations(nuRes.violations || []);
      setUsersNotInCatalog(nuRes.users_not_in_catalog || []);
      setUsersBlankCatalogDefault(nuRes.users_blank_catalog_default || []);
      if (Object.keys(nuRes.limits || {}).length) setNumUsersLimits(nuRes.limits);
    } catch { /* non-fatal */ }
    await recordOperatorOverride({
      action: 'cap_users_to_catalog_max',
      summary: `Capped Users to catalog max on ${n} row(s)`,
      detail: numUsersViolations.map((v) => `${v.ci_name}: ${v.requested_users}→${v.maximum}`).join('; '),
      affected_count: n,
    });
    showToast(`Capped Users to catalog max on ${n} row(s) (local only — not Labagator)`, 'info');
  };

  const exportSkippedCsv = () => {
    if (!skippedCatalogSummary?.removedSchedules?.length) {
      showToast('Nothing skipped to export', 'danger');
      return;
    }
    const headers = [
      'workshop_name', 'ci', 'ci_name', 'namespace', 'multi_workshop_name', 'asset_cis',
      'provisioning_date', 'auto_stop', 'auto_destroy', 'skip_reason',
    ];
    const reasonByCi = new Map(skippedCatalogSummary.items.map((i) => [i.ci, i.reason]));
    const rows = skippedCatalogSummary.removedSchedules.map((s) => {
      const assetHit = (s.asset_cis || '')
        .split(',')
        .map((a) => a.trim())
        .find((a) => reasonByCi.has(a));
      const reason = reasonByCi.get(s.ci) || (assetHit ? reasonByCi.get(assetHit) : '') || 'not published on this cluster';
      return [
        s.workshop_name, s.ci, s.ci_name, s.namespace, s.multi_workshop_name, s.asset_cis,
        s.provisioning_date, s.auto_stop, s.auto_destroy, reason,
      ];
    });
    downloadCsv(`flow-skipped-missing-cis-${stampForFilename()}.csv`, headers, rows);
    showToast(`Exported ${rows.length} skipped row(s)`, 'info');
  };

  const exportScheduleCsv = () => {
    if (schedules.length === 0) {
      showToast('No schedules to export', 'danger');
      return;
    }
    const headers = [
      'workshop_name', 'ci', 'ci_name', 'namespace', 'catalog_namespace', 'users', 'instances',
      'concurrency', 'password', 'activity', 'purpose', 'provisioning_date', 'auto_stop', 'auto_destroy',
      'enable_workshop_interface', 'is_multi_asset', 'multi_workshop_name', 'asset_cis',
      'salesforce_ids', 'salesforce_type', 'aws_regions', 'white_glove', 'redirect',
      'showroom_repo', 'showroom_ref', 'item_type', 'cluster_link', 'cluster_ci_override',
      'is_cluster', 'is_tenant', 'detected_cluster_ci',
    ];
    const rows = schedules.map((s) => [
      s.workshop_name, s.ci, s.ci_name, s.namespace, s.catalog_namespace, s.users, s.instances,
      s.concurrency, s.password, s.activity, s.purpose, s.provisioning_date, s.auto_stop, s.auto_destroy,
      s.enable_workshop_interface, s.is_multi_asset, s.multi_workshop_name, s.asset_cis,
      s.salesforce_ids, s.salesforce_type, s.aws_regions, s.white_glove, s.redirect,
      s.showroom_repo, s.showroom_ref, s.item_type ?? '', s.cluster_link ?? '', s.cluster_ci_override ?? '',
      s.is_cluster ?? '', s.is_tenant ?? '', s.detected_cluster_ci ?? '',
    ]);
    downloadCsv(`flow-schedules-${stampForFilename()}.csv`, headers, rows);
    showToast(`Exported ${rows.length} schedule row(s)`, 'info');
  };

  const exportDeployResultsCsv = () => {
    if (!results?.length) {
      showToast('No deploy results yet — run a deploy first', 'danger');
      return;
    }
    const headers = [
      'ci_name', 'ci', 'namespace', 'guid', 'url', 'status', 'error_message',
      'provisioning_date', 'auto_stop', 'auto_destroy', 'timestamp', 'password',
      'users', 'instances', 'showroom_url', 'showroom_status',
    ];
    const rows = results.map((r) => [
      r.ci_name, r.ci, r.namespace, r.guid, r.url, r.status, r.error_message ?? '',
      r.provisioning_date, r.auto_stop, r.auto_destroy, r.timestamp, r.password ?? '',
      r.users ?? '', r.instances ?? '', r.showroom_url ?? '', r.showroom_status ?? '',
    ]);
    downloadCsv(`flow-deploy-results-${stampForFilename()}.csv`, headers, rows);
    showToast(`Exported ${rows.length} deploy result(s)`, 'info');
  };

  return (
    <PageSection>
      {/* Import format toggle */}
      <div style={{ marginBottom: 16 }}>
        <ToggleGroup aria-label="Import format">
          <ToggleGroupItem
            text="Flow CSV"
            buttonId="flow-format"
            isSelected={importMode === 'flow'}
            onChange={() => setImportMode('flow')}
          />
          <ToggleGroupItem
            text="Labagator Sessions"
            buttonId="labagator-format"
            isSelected={importMode === 'labagator'}
            onChange={() => setImportMode('labagator')}
          />
        </ToggleGroup>
      </div>

      {importMode === 'labagator' && (
        <>
          <Alert
            variant="info"
            isInline
            title="Labagator import mode"
            style={{ marginBottom: 12 }}
          >
            Import a Labagator sessions CSV export. Settings below will be applied to all imported sessions.
          </Alert>
          <Card style={{ marginBottom: 16 }}>
            <CardTitle>Labagator Import Settings</CardTitle>
            <CardBody>
              <Split hasGutter style={{ marginBottom: 12 }}>
                <SplitItem>
                  <label htmlFor="labagator-ci">Default Catalog Item:</label>
                  <TextInput
                    id="labagator-ci"
                    value={labagatorDefaultCI}
                    onChange={(_e, value) => setLabagatorDefaultCI(value)}
                    placeholder="e.g., ocp4-cluster.prod"
                    style={{ width: '300px' }}
                  />
                </SplitItem>
                <SplitItem>
                  <label htmlFor="labagator-users">Default Users:</label>
                  <TextInput
                    id="labagator-users"
                    type="number"
                    value={labagatorDefaultUsers.toString()}
                    onChange={(_e, value) => setLabagatorDefaultUsers(parseInt(value) || 25)}
                    style={{ width: '100px' }}
                  />
                </SplitItem>
                <SplitItem>
                  <label htmlFor="labagator-buffer">Destroy Buffer (hours):</label>
                  <TextInput
                    id="labagator-buffer"
                    type="number"
                    value={labagatorBufferHours.toString()}
                    onChange={(_e, value) => setLabagatorBufferHours(parseInt(value) || 2)}
                    style={{ width: '100px' }}
                  />
                </SplitItem>
              </Split>
              <Split hasGutter>
                <SplitItem>
                  <Tooltip content="Apply global redirect setting to imported sessions">
                    <Switch
                      id="labagator-redirect-inherit"
                      label="Use global redirect setting"
                      isChecked={true}
                      isDisabled
                    />
                  </Tooltip>
                </SplitItem>
                <SplitItem>
                  <Tooltip content="Apply global white glove setting to imported sessions">
                    <Switch
                      id="labagator-whiteglove-inherit"
                      label="Use global white glove setting"
                      isChecked={true}
                      isDisabled
                    />
                  </Tooltip>
                </SplitItem>
              </Split>
            </CardBody>
          </Card>
        </>
      )}

      {/* CSV Upload */}
      <Card style={{ marginBottom: 16 }}>
        <CardTitle>Upload Flow CSV</CardTitle>
        <CardBody>
          <Split hasGutter style={{ alignItems: 'center' }}>
            <SplitItem isFilled>
              <FileUpload
                id="csv-file-upload"
                filename={csvFilename}
                filenamePlaceholder="Drag & drop or browse for a CSV file"
                browseButtonText="Browse"
                clearButtonText="Clear"
                onFileInputChange={(_e, file) => { setCsvFile(file); setCsvFilename(file.name); }}
                onClearClick={() => { setCsvFile(null); setCsvFilename(''); }}
                dropzoneProps={{ accept: { 'text/csv': ['.csv'] } }}
                hideDefaultPreview
              />
            </SplitItem>
            <SplitItem>
              <Button variant="primary" onClick={handleUpload}>Upload</Button>
            </SplitItem>
            <SplitItem>
              <Button variant="secondary" onClick={() => setShowClearConfirm(true)}>Clear / New Upload</Button>
            </SplitItem>
          </Split>
        </CardBody>
      </Card>

      {/* Import from Labagator */}
      <Card style={{ marginBottom: 16 }}>
        <CardTitle>Import from Labagator</CardTitle>
        <CardBody>
          {labagatorUnavailable ? (
            <Alert variant="warning" isInline title="Labagator is unavailable — use manual CSV export instead" />
          ) : (
            <>
              <Split hasGutter style={{ marginBottom: 12, alignItems: 'flex-end' }}>
                <SplitItem>
                  <FormSelect
                    aria-label="Event look-ahead window"
                    value={labagatorRangeDays}
                    onChange={(_e, v) => setLabagatorRangeDays(Number(v))}
                    style={{ minWidth: 160 }}
                  >
                    <FormSelectOption value={7} label="Next 7 days" />
                    <FormSelectOption value={30} label="Next 30 days" />
                    <FormSelectOption value={90} label="Next 90 days" />
                    <FormSelectOption value={0} label="All upcoming" />
                  </FormSelect>
                </SplitItem>
                <SplitItem isFilled>
                  <FormSelect
                    aria-label="Labagator event"
                    value={selectedEventId ?? ''}
                    onChange={(_e, v) => setSelectedEventId(v ? Number(v) : null)}
                    isDisabled={labagatorEventsLoading || labagatorEvents.length === 0}
                  >
                    <FormSelectOption
                      key=""
                      value=""
                      label={
                        labagatorEventsLoading
                          ? 'Loading events…'
                          : labagatorEvents.length === 0
                            ? 'No events in this range'
                            : 'Select an event…'
                      }
                    />
                    {labagatorEvents.map((ev) => (
                      <FormSelectOption key={ev.id} value={ev.id} label={`${ev.name} (${ev.start_date} – ${ev.end_date})`} />
                    ))}
                  </FormSelect>
                </SplitItem>
                <SplitItem isFilled>
                  <TextInput
                    id="labagator-namespace"
                    aria-label="Namespace"
                    placeholder="Namespace (required)"
                    value={labagatorNamespace}
                    onChange={(_e, v) => { setLabagatorNamespace(v); setLabagatorNamespaceError(''); }}
                    validated={labagatorNamespaceError ? 'error' : 'default'}
                  />
                </SplitItem>
                <SplitItem>
                  <Button
                    variant="secondary"
                    isDisabled={selectedEventId === null || labagatorSessionsLoading}
                    isLoading={labagatorSessionsLoading}
                    onClick={handleFetchLabagatorSessions}
                  >
                    Fetch sessions
                  </Button>
                </SplitItem>
                <SplitItem>
                  <Button
                    variant="primary"
                    isDisabled={!labagatorNamespace || labagatorPreviewing || labagatorEvents.length === 0}
                    isLoading={labagatorPreviewing}
                    onClick={handleLabagatorPreview}
                  >
                    Import
                  </Button>
                </SplitItem>
              </Split>
              {!labagatorEventsLoading && labagatorEvents.length === 0 && (
                <Alert
                  variant="info"
                  isInline
                  title="No Labagator events returned for this window"
                  style={{ marginBottom: 12 }}
                >
                  Prefer Labagator → Deploy Handoff → <strong>Review &amp; Deploy in Flow</strong> for
                  session-selected deploys. This import lists upcoming events from Labagator&apos;s API —
                  widen the look-ahead, or check Flow&apos;s <code>LABAGATOR_BASE_URL</code> points at the
                  right instance.
                </Alert>
              )}
              {labagatorNamespaceError && (
                <Alert variant="danger" isInline title={labagatorNamespaceError} style={{ marginBottom: 12 }} />
              )}

              {labagatorSessionsFetched && (
                <div style={{ marginBottom: 12, maxHeight: 240, overflowY: 'auto', border: '1px solid var(--pf-t--global--border--color--default)', padding: 8 }}>
                  {labagatorSessions.length === 0 ? (
                    <Alert variant="info" isInline title="No deployable sessions for this event" />
                  ) : (
                    <>
                      <Checkbox
                        id="labagator-select-all-sessions"
                        label={`Select all (${selectedSessionIds.size} of ${labagatorSessions.length})`}
                        isChecked={allSessionsSelected}
                        onChange={(_e, checked) => toggleSelectAllSessions(checked)}
                        style={{ marginBottom: 8 }}
                      />
                      {labagatorSessions.map((s) => (
                        <Checkbox
                          key={s.room_session_id}
                          id={`labagator-session-${s.room_session_id}`}
                          isChecked={selectedSessionIds.has(s.room_session_id)}
                          onChange={(_e, checked) => toggleSession(s.room_session_id, checked)}
                          label={`${s.date} · ${s.title || s.ci_name || `session ${s.room_session_id}`}${s.users ? ` · ${s.users} users` : ''}${s.deploy_on ? ` · deploy ${s.deploy_on}` : ''}`}
                          description={s.ci_name && s.title ? s.ci_name : undefined}
                          style={{ marginBottom: 4 }}
                        />
                      ))}
                    </>
                  )}
                </div>
              )}

              <ExpandableSection
                toggleText="Advanced"
                isExpanded={labagatorAdvancedOpen}
                onToggle={() => setLabagatorAdvancedOpen(!labagatorAdvancedOpen)}
              >
                <Split hasGutter style={{ marginTop: 12 }}>
                  <SplitItem>
                    <Switch
                      id="labagator-enable-workshop-interface"
                      label="Enable workshop interface"
                      isChecked={labagatorEnableWorkshopInterface}
                      onChange={(_e, checked) => setLabagatorEnableWorkshopInterface(checked)}
                    />
                  </SplitItem>
                  <SplitItem>
                    <NumberInput
                      value={labagatorConcurrency}
                      min={1}
                      onMinus={() => setLabagatorConcurrency((n) => Math.max(1, n - 1))}
                      onPlus={() => setLabagatorConcurrency((n) => n + 1)}
                      onChange={(e) => setLabagatorConcurrency(Number((e.target as HTMLInputElement).value) || 1)}
                      inputAriaLabel="Concurrency"
                      widthChars={4}
                    />
                  </SplitItem>
                  <SplitItem>
                    <NumberInput
                      value={labagatorAutoStopDays}
                      min={0}
                      onMinus={() => setLabagatorAutoStopDays((n) => Math.max(0, n - 1))}
                      onPlus={() => setLabagatorAutoStopDays((n) => n + 1)}
                      onChange={(e) => setLabagatorAutoStopDays(Number((e.target as HTMLInputElement).value) || 0)}
                      inputAriaLabel="Auto-stop days"
                      widthChars={4}
                    />
                  </SplitItem>
                  <SplitItem>
                    <NumberInput
                      value={labagatorAutoDestroyDays}
                      min={0}
                      onMinus={() => setLabagatorAutoDestroyDays((n) => Math.max(0, n - 1))}
                      onPlus={() => setLabagatorAutoDestroyDays((n) => n + 1)}
                      onChange={(e) => setLabagatorAutoDestroyDays(Number((e.target as HTMLInputElement).value) || 0)}
                      inputAriaLabel="Auto-destroy days"
                      widthChars={4}
                    />
                  </SplitItem>
                </Split>
              </ExpandableSection>
            </>
          )}
        </CardBody>
      </Card>

      {/* Passwords CSV upload */}
      <Split hasGutter style={{ marginBottom: 16, alignItems: 'center' }}>
        <SplitItem isFilled>
          <FileUpload
            id="password-file-upload"
            filename={passwordFilename}
            filenamePlaceholder="Drag & drop or browse for a passwords CSV"
            browseButtonText="Browse"
            clearButtonText="Clear"
            onFileInputChange={(_e, file) => { setPasswordFile(file); setPasswordFilename(file.name); }}
            onClearClick={() => { setPasswordFile(null); setPasswordFilename(''); }}
            dropzoneProps={{ accept: { 'text/csv': ['.csv'] } }}
            hideDefaultPreview
          />
        </SplitItem>
        <SplitItem>
          <Button variant="secondary" onClick={handleUploadPasswords}>Upload Passwords</Button>
        </SplitItem>
        {passwordCount !== null && (
          <SplitItem>
            <span>{passwordCount} asset password(s) loaded</span>
          </SplitItem>
        )}
      </Split>

      {/* Schedule preview */}
      {schedules.length > 0 ? (
        <>
          <Split hasGutter style={{ marginBottom: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <SplitItem>
              <Title headingLevel="h3">
                Schedule Preview ({previewSearch ? `${filteredSchedules.length} of ${schedules.length}` : schedules.length})
              </Title>
            </SplitItem>
            <SplitItem isFilled />
            <SplitItem>
              <Tooltip content="When on, row dates, per-row redirect, and delete are disabled. Expand rows still works.">
                <Switch
                  id="schedule-row-edits-lock"
                  label="Lock row edits"
                  isChecked={rowEditsLocked}
                  onChange={(_e, c) => setRowEditsLocked(c)}
                  isReversed
                />
              </Tooltip>
            </SplitItem>
            <SplitItem>
              <SearchInput
                placeholder="Search schedules..."
                value={previewSearch}
                onChange={(_e, val) => setPreviewSearch(val)}
                onClear={() => setPreviewSearch('')}
                style={{ width: 220 }}
              />
            </SplitItem>
            <SplitItem>
              <Button variant="link" component="a" href={api.templateURL}>
                Download CSV Template
              </Button>
            </SplitItem>
            <SplitItem>
              <Tooltip content="Opens a new browser tab (#edit) with all CSV fields per row. Shares the same API session; use Save there, then reload this page to refresh the table.">
                <Button
                  variant="secondary"
                  onClick={() => {
                    const u = new URL(window.location.href);
                    u.hash = 'edit';
                    window.open(u.toString(), '_blank', 'noopener,noreferrer');
                  }}
                  isDisabled={schedules.length === 0}
                >
                  Full editor (new tab)
                </Button>
              </Tooltip>
            </SplitItem>
          </Split>
          {scheduleExamples.length > 0 && (
            <div style={{ marginBottom: 10, fontSize: '0.875rem', display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '4px 8px' }}>
              <span style={{ color: 'var(--pf-v6-global--Color--200)' }}>Load example:</span>
              {scheduleExamples.map((ex) => (
                <Button
                  key={ex.slug}
                  variant="link"
                  isInline
                  isDisabled={!!loadingExampleSlug || deploying || validating || yamlDownloading}
                  isLoading={loadingExampleSlug === ex.slug}
                  onClick={() => handleLoadExample(ex.slug)}
                >
                  {ex.label}
                </Button>
              ))}
            </div>
          )}

          {/* Skipped rows warning */}
          {skippedRows > 0 && (
            <Alert variant="danger" isInline title={`${skippedRows} of ${totalRows} CSV row(s) were skipped`} style={{ marginBottom: 12 }}>
              Some rows could not be parsed (bad values in Users, Instances, Concurrency, or missing required fields).
              Review the source CSV and re-upload.
            </Alert>
          )}

          {/* Validation warnings — compact summary, expand for details */}
          {warnings.length > 0 && (
            <Alert
              variant="warning"
              isInline
              title={
                conflictWarnings.length && !otherWarnings.length
                  ? `${conflictWarnings.length} same-day CI+Namespace conflict(s) — different days are OK`
                  : `${warnings.length} validation warning(s) — review before deploying`
              }
              style={{ marginBottom: 12 }}
            >
              <p style={{ margin: '0 0 8px', fontSize: '0.85rem' }}>
                {conflictWarnings.length > 0 && (
                  <>{conflictWarnings.length} lab(s) have multiple sessions on the same UTC day in one namespace. </>
                )}
                {otherWarnings.length > 0 && <>{otherWarnings.length} other date/config issue(s). </>}
                Non-blocking — expand for row details.
              </p>
              <ExpandableSection
                toggleText={warningsExpanded ? 'Hide details' : 'Show details'}
                onToggle={(_e, open) => setWarningsExpanded(open)}
                isExpanded={warningsExpanded}
              >
                <ul style={{ margin: '4px 0 0', paddingLeft: 20, fontSize: '0.85rem' }}>
                  {warnings.map((w, i) => <li key={i}>{w.message}</li>)}
                </ul>
              </ExpandableSection>
              {hasMissingDateWarnings && (
                <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid var(--pf-v6-global--BorderColor--100)' }}>
                  <Button variant="secondary" onClick={() => setShowFillDatesModal(true)} size="sm">
                    Fill Missing Dates Globally
                  </Button>
                  <span style={{ marginLeft: 12, fontSize: '0.85rem', color: 'var(--pf-v6-global--Color--200)' }}>
                    Set default dates for all schedules with missing provisioning, stop, or destroy dates
                  </span>
                </div>
              )}
            </Alert>
          )}

          {/* Namespace existence warning */}
          {missingNamespaces.length > 0 && (
            <Alert variant="danger" isInline title={`${missingNamespaces.length} namespace(s) not found on cluster`} style={{ marginBottom: 12 }}>
              The following namespaces do not exist: <strong>{missingNamespaces.join(', ')}</strong>.
              Deployment will fail unless these are created first.
            </Alert>
          )}

          {/* num_users limit violations */}
          {numUsersViolations.length > 0 && (
            <Alert
              variant="danger"
              isInline
              title={`${numUsersViolations.length} schedule(s) exceed num_users limit`}
              style={{ marginBottom: 12 }}
              actionLinks={
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  <Button variant="secondary" size="sm" onClick={() => requestCapUsers()}>
                    Cap Users to catalog max
                  </Button>
                  <Button
                    variant="link"
                    size="sm"
                    onClick={() => setNumUsersViolations([])}
                  >
                    Dismiss
                  </Button>
                </div>
              }
            >
              <AffectsItemsList
                count={numUsersViolations.length}
                expanded={numUsersExpanded}
                onToggle={() => setNumUsersExpanded((v) => !v)}
                noun="schedule"
              >
                {sortByCiName(numUsersViolations).map((v, i) => (
                  <li key={i}>
                    <strong>{v.ci_name}</strong> ({v.ci}): {v.requested_users} users requested, catalog max is {v.maximum}
                  </li>
                ))}
              </AffectsItemsList>
              Deployment is blocked until counts are at or below the catalog max. Cap is a local Flow tweak for testing — Labagator remains the event source of truth.
            </Alert>
          )}

          {/* Catalog parameter schema violations (e.g. AWS_Region not in enum) */}
          {paramViolations.length > 0 && (
            <Alert
              variant="danger"
              isInline
              title={`${paramViolations.length} schedule(s) have invalid catalog parameters`}
              style={{ marginBottom: 12 }}
              actionLinks={
                <Button variant="link" size="sm" onClick={() => setParamViolations([])}>
                  Dismiss
                </Button>
              }
            >
              <AffectsItemsList
                count={paramViolations.length}
                expanded={paramViolationsExpanded}
                onToggle={() => setParamViolationsExpanded((v) => !v)}
                noun="schedule"
              >
                {sortByCiName(paramViolations).map((v, i) => (
                  <li key={i}>
                    <strong>{v.ci_name}</strong> ({v.ci}): {v.message}
                  </li>
                ))}
              </AffectsItemsList>
              Deployment is blocked: these values violate the catalog item&apos;s schema and Babylon would reject them at provision time. Fix the value in the CSV (e.g. set AWS_Region to an allowed region, or leave it blank to use the catalog default).
            </Alert>
          )}

          {/* Catalog parameter advisories (unknown param / required without default) */}
          {paramWarnings.length > 0 && (
            <Alert
              variant="warning"
              isInline
              title={`${paramWarnings.length} catalog parameter advisory(ies)`}
              style={{ marginBottom: 12 }}
              actionLinks={
                <Button variant="link" size="sm" onClick={() => setParamWarnings([])}>
                  Dismiss
                </Button>
              }
            >
              <AffectsItemsList
                count={paramWarnings.length}
                expanded={paramWarningsExpanded}
                onToggle={() => setParamWarningsExpanded((v) => !v)}
                noun="schedule"
              >
                {sortByCiName(paramWarnings).map((v, i) => (
                  <li key={i}>
                    <strong>{v.ci_name}</strong> ({v.ci}): {v.message}
                  </li>
                ))}
              </AffectsItemsList>
              These are advisory only — deployment is not blocked.
            </Alert>
          )}

          {/* Pool capacity warnings */}
          {(poolCapacityWarnings.length > 0 || poolsNotFound.length > 0)
           && !(missingTenantRefs && poolCapacityWarnings.length === 0) && (
            <Alert
              variant={poolCapacityWarnings.some(w => w.severity === 'critical') ? 'danger' : 'info'}
              isInline
              title={`TenantClusterPool capacity: ${poolCapacityWarnings.length} warning(s), ${poolsNotFound.length} pool(s) not found`}
              style={{ marginBottom: 12 }}
            >
              {poolCapacityWarnings.length > 0 && (
                <>
                  <div style={{ marginBottom: 4, fontWeight: 600 }}>Capacity warnings</div>
                  <AffectsItemsList
                    count={poolCapacityWarnings.length}
                    expanded={poolWarnExpanded}
                    onToggle={() => setPoolWarnExpanded((v) => !v)}
                  >
                    {sortByCiName(poolCapacityWarnings).map((w, i) => (
                      <li key={i} style={{ color: w.severity === 'critical' ? 'var(--pf-v6-global--danger-color--100)' : undefined }}>
                        <strong>{w.ci_name}</strong> ({w.pool_name}):
                        Pool {w.pool_saturation_percent}% saturated, {w.placement_capacity_percent}% utilized
                        {w.severity === 'critical' && ' — CRITICAL: at capacity!'}
                      </li>
                    ))}
                  </AffectsItemsList>
                </>
              )}
              {poolsNotFound.length > 0 && (
                <>
                  <div style={{ marginTop: poolCapacityWarnings.length > 0 ? 8 : 0, marginBottom: 4, fontWeight: 600 }}>
                    Pools not found
                  </div>
                  <AffectsItemsList
                    count={poolsNotFound.length}
                    expanded={poolWarnExpanded}
                    onToggle={() => setPoolWarnExpanded((v) => !v)}
                  >
                    {sortByCiName(poolsNotFound).map((p, i) => (
                      <li key={i}>
                        <strong>{p.ci_name}</strong>
                      </li>
                    ))}
                  </AffectsItemsList>
                  <div style={{ marginTop: 4, fontSize: '0.85rem', fontStyle: 'italic', color: 'var(--pf-v6-global--Color--200)' }}>
                    These workshops can deploy using fresh cluster instances if needed (slower provisioning).
                  </div>
                </>
              )}
            </Alert>
          )}

          {/* Catalog item has no num_users but CSV sets Users (e.g. use Instances for WorkshopProvision) */}
          {usersNotInCatalog.length > 0 && (
            <Alert
              variant={usersNotInCatalog.some(a => a.severity === 'high') ? 'danger' : 'info'}
              isInline
              title={`${usersNotInCatalog.length} row(s): Users set but catalog item has no num_users`}
              style={{ marginBottom: 12 }}
              actionLinks={
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  <Tooltip content="Copy Users into Instances, then clear Users. Local schedule only — does not change Labagator.">
                    <Button variant="secondary" size="sm" onClick={() => requestUsersFix('move-to-instances')}>
                      Move Users → Instances
                    </Button>
                  </Tooltip>
                  <Tooltip content="Clear Users and keep the current Instances value (common when Instances is already correct). Local only.">
                    <Button variant="secondary" size="sm" onClick={() => requestUsersFix('clear-users')}>
                      Clear Users (keep Instances)
                    </Button>
                  </Tooltip>
                  <Button
                    variant="link"
                    size="sm"
                    onClick={() => setUsersNotInCatalog([])}
                  >
                    Dismiss
                  </Button>
                </div>
              }
            >
              <AffectsItemsList
                count={usersNotInCatalog.length}
                expanded={usersAdvisoryExpanded}
                onToggle={() => setUsersAdvisoryExpanded((v) => !v)}
                noun="row"
              >
                {sortByCiName(usersNotInCatalog).map((a, i) => (
                  <li key={i}>{a.message}</li>
                ))}
              </AffectsItemsList>
              <div style={{ fontSize: '0.85rem', color: 'var(--pf-v6-global--Color--200)' }}>
                Labagator is still the master plan for the event. Use the buttons above only to unblock testing / partial deploys in Flow.
              </div>
            </Alert>
          )}

          {/* Users blank but catalog has num_users — info only, never blocks deploy */}
          {usersBlankCatalogDefault.length > 0 && (
            <Alert
              variant="info"
              isInline
              title={`${usersBlankCatalogDefault.length} row(s): Users blank — will use catalog default seats`}
              style={{ marginBottom: 12 }}
              actionLinks={
                <Button variant="link" size="sm" onClick={() => setUsersBlankCatalogDefault([])}>
                  Dismiss
                </Button>
              }
            >
              <div style={{ fontSize: '0.9rem', marginBottom: 8 }}>
                Shared multiuser labs need <strong>Users</strong> for seats (and usually <strong>Instances=1</strong>).
                Deploy still proceeds with the catalog default — set Users if that default is too small.
              </div>
              <AffectsItemsList
                count={usersBlankCatalogDefault.length}
                expanded={usersBlankExpanded}
                onToggle={() => setUsersBlankExpanded((v) => !v)}
                noun="row"
              >
                {sortByCiName(usersBlankCatalogDefault).map((a, i) => (
                  <li key={i}>{a.message}</li>
                ))}
              </AffectsItemsList>
            </Alert>
          )}

          {/* Catalog namespace mismatches — auto-corrected (CI exists, wrong NS column) */}
          {catalogNamespaceMismatches.length > 0 && (
            <Alert
              variant="info"
              isInline
              title={`Catalog namespace auto-corrected for ${catalogNamespaceMismatches.length} item(s)`}
              style={{ marginBottom: 12 }}
            >
              <div style={{ fontSize: '0.9rem', marginBottom: 8 }}>
                These CIs already exist — the schedule pointed at the wrong catalog namespace.
                Flow auto-corrected locally so deploy works — <strong>still fix Labagator</strong> so the
                next export ships the right namespace (otherwise this warning returns).
              </div>
              <AffectsItemsList
                count={catalogNamespaceMismatches.length}
                expanded={catalogNsExpanded}
                onToggle={() => setCatalogNsExpanded((v) => !v)}
              >
                {sortByCiName(catalogNamespaceMismatches).map((m, i) => (
                  <li key={i}>
                    <strong>{m.ci_name}</strong>: not in <code>{m.expected_catalog_namespace}</code>, found in <code>{m.found_catalog_namespace}</code> — deploy will use {m.found_catalog_namespace}
                  </li>
                ))}
              </AffectsItemsList>
              <div style={{ fontSize: '0.85rem', color: 'var(--pf-v6-global--Color--200)' }}>
                Flow namespace column updated for this session. Correct the catalog namespace / CI stage in Labagator when you can.
              </div>
            </Alert>
          )}

          {/* Using .prod instead of .event — info only, never blocks deploy */}
          {prodNotEvent.length > 0 && (
            <Alert
              variant="info"
              isInline
              title={`${prodNotEvent.length} item(s) using .prod (not .event) — deploy OK`}
              style={{ marginBottom: 12 }}
              actionLinks={
                <Button variant="link" size="sm" onClick={() => setProdNotEvent([])}>
                  Dismiss
                </Button>
              }
            >
              <div style={{ fontSize: '0.9rem', marginBottom: 8 }}>
                Big-event default prefers <code>.event</code> when published. These rows will deploy
                via <code>.prod</code> — <strong>this does not block deployment</strong>. Still worth
                correcting upstream so Labagator / the next export match the event stage.
              </div>
              <div style={{ fontSize: '0.9rem', marginBottom: 6, fontWeight: 600 }}>Suggested fixes:</div>
              <ol style={{ margin: '0 0 10px 20px', fontSize: '0.9rem' }}>
                <li>
                  <strong>Preferred:</strong> add <code>event.yaml</code> in agnosticv → publish{' '}
                  <code>babylon-catalog-event/&lt;ci&gt;.event</code>, then re-export from Labagator
                </li>
                <li>
                  <strong>Or</strong> set the CI suffix / catalog stage in Labagator so the master plan
                  ships <code>.prod</code> intentionally (not as a silent default)
                </li>
                <li>
                  <strong>Keep .prod</strong> for this Flow run — deploy proceeds as-is
                </li>
              </ol>
              <AffectsItemsList
                count={prodNotEvent.length}
                expanded={prodNotEventExpanded}
                onToggle={() => setProdNotEventExpanded((v) => !v)}
              >
                {sortByCiName(prodNotEvent).map((a) => (
                  <li key={a.ci}>
                    <strong>{a.ci_name}</strong>: <code>{a.ci}</code>
                    {a.event_published ? ' (.event also published)' : ' (no .event published)'}
                  </li>
                ))}
              </AffectsItemsList>
            </Alert>
          )}

          {/* CI suffix auto-correct — .event when published, else .prod */}
          {catalogSuffixCorrections.length > 0 && (
            <Alert
              variant="info"
              isInline
              title={`CI suffix auto-corrected for ${catalogSuffixCorrections.length} item(s)`}
              style={{ marginBottom: 12 }}
              actionLinks={
                <Button variant="link" size="sm" onClick={() => setCatalogSuffixCorrections([])}>
                  Dismiss
                </Button>
              }
            >
              <div style={{ fontSize: '0.9rem', marginBottom: 8 }}>
                Schedule CI was bare. Flow looked for a published env suffix:{' '}
                <code>.event</code> if it exists, otherwise <code>.prod</code>.
                {' '}<strong>Local Flow only</strong> — diverges from Labagator when the master plan differs.
              </div>
              {catalogSuffixCorrections.some((c) => c.corrected_ci.endsWith('.prod')) && (
                <>
                  <div style={{ fontSize: '0.9rem', marginBottom: 6, fontWeight: 600 }}>
                    No <code>.event</code> for some of these — options:
                  </div>
                  <ol style={{ margin: '0 0 10px 20px', fontSize: '0.9rem' }}>
                    <li>
                      <strong>Preferred:</strong> add <code>event.yaml</code> in agnosticv → publish{' '}
                      <code>babylon-catalog-event/&lt;ci&gt;.event</code>
                    </li>
                    <li>
                      <strong>Keep auto .prod</strong> (already applied on those rows)
                    </li>
                    <li>
                      <strong>Fix Labagator</strong> so the master plan ships the correct CI suffix
                    </li>
                    <li>
                      <strong>Skip</strong> items you do not want (remaining-suffix alert if any)
                    </li>
                  </ol>
                </>
              )}
              {catalogSuffixCorrections.some((c) => c.corrected_ci.endsWith('.event')) && (
                <div style={{ fontSize: '0.85rem', marginBottom: 8, color: 'var(--pf-v6-global--Color--200)' }}>
                  Rows auto-set to <code>.event</code> — preferred for big events. Still a Flow override if Labagator said otherwise.
                </div>
              )}
              <AffectsItemsList
                count={catalogSuffixCorrections.length}
                expanded={catalogSuffixExpanded}
                onToggle={() => setCatalogSuffixExpanded((v) => !v)}
              >
                {sortByCiName(catalogSuffixCorrections).map((c) => (
                  <li key={c.ci}>
                    <code>{c.ci}</code> → <code>{c.corrected_ci}</code>
                  </li>
                ))}
              </AffectsItemsList>
            </Alert>
          )}

          {/* Truly missing / ambiguous (rare after auto .event/.prod) */}
          {catalogNotFoundUnique.length > 0 && (
            <Alert
              variant="warning"
              isInline
              title={`${catalogNotFoundUnique.length} catalog item(s) still missing`}
              style={{ marginBottom: 12 }}
              actionLinks={
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  {catalogHasEventOpt.length > 0 && (
                    <Button variant="secondary" size="sm" onClick={() => requestApplyEventWhereAvailable()}>
                      Use .event ({catalogHasEventOpt.length})
                    </Button>
                  )}
                  {catalogRemainingWithProd.length > 0 && (
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => {
                        setRiskConfirm({
                          kind: 'apply-prod-no-event',
                          title: 'Use .prod for these CIs?',
                          body: `Rewrite ${catalogRemainingWithProd.length} bare CI(s) to .prod. Local Flow — diverges from Labagator.`,
                          bullets: catalogRemainingWithProd.slice(0, 8).map((n) => `${n.ci} → ${n.ci}.prod`),
                        });
                      }}
                    >
                      Use .prod ({catalogRemainingWithProd.length})
                    </Button>
                  )}
                  <Button variant="primary" size="sm" onClick={() => requestSkipMissing()}>
                    Skip these items
                  </Button>
                </div>
              }
            >
              <div style={{ fontSize: '0.9rem', marginBottom: 8 }}>
                Exact CI name not published and Flow could not auto-pick. Any manual pick here also diverges from Labagator.
              </div>
              <AffectsItemsList
                count={catalogNotFoundUnique.length}
                expanded={catalogNotFoundExpanded}
                onToggle={() => setCatalogNotFoundExpanded((v) => !v)}
              >
                {sortByCiName(catalogNotFoundUnique).map((nf) => (
                  <li key={nf.ci}>
                    <code>{nf.ci}</code>
                    {catalogSuffixOpts(nf).length > 0 ? (
                      <span style={{ color: 'var(--pf-v6-global--Color--200)' }}>
                        {' '}— {catalogSuffixOpts(nf).map((o) => o.split('.').pop()).join(' / ')}
                      </span>
                    ) : null}
                  </li>
                ))}
              </AffectsItemsList>
            </Alert>
          )}

          {skippedCatalogSummary && skippedCatalogSummary.items.length > 0 && (
            <Alert
              variant="info"
              isInline
              title={`Skipped ${skippedCatalogSummary.items.length} missing catalog item(s) (${skippedCatalogSummary.rows} schedule row${skippedCatalogSummary.rows === 1 ? '' : 's'}) — ${skippedCatalogSummary.remaining} remain`}
              style={{ marginBottom: 12 }}
              actionLinks={
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  <Button variant="secondary" size="sm" onClick={exportSkippedCsv}>
                    Export skipped CSV
                  </Button>
                  <Button variant="link" size="sm" onClick={() => setSkippedCatalogSummary(null)}>
                    Dismiss
                  </Button>
                </div>
              }
            >
              <AffectsItemsList
                count={skippedCatalogSummary.items.length}
                expanded={skippedItemsExpanded}
                onToggle={() => setSkippedItemsExpanded((v) => !v)}
              >
                {sortByCiName(skippedCatalogSummary.items).map((item) => (
                  <li key={item.ci}>
                    <strong>{item.ci_name}</strong> <code>{item.ci}</code>
                    {item.reason ? (
                      <span style={{ color: 'var(--pf-v6-global--Color--200)' }}> — {item.reason}</span>
                    ) : null}
                  </li>
                ))}
              </AffectsItemsList>
            </Alert>
          )}

          {/* Cluster capacity needs */}
          {clusterNeeds && clusterNeeds.total_deficit > 0 && !missingTenantRefs && (
            <Alert
              variant="warning"
              isInline
              title={`Need ${clusterNeeds.total_deficit} more cluster(s) for ${clusterNeeds.total_tenant_count} tenant workshops`}
              style={{ marginBottom: 12 }}
            >
              <div style={{ marginBottom: 8 }}>
                You're deploying tenant workshops but don't have enough cluster CIs in your CSV:
              </div>
              <ul style={{ margin: '0 0 10px 20px', fontSize: '0.9rem' }}>
                {clusterNeeds.needs.filter((n: any) => n.deficit > 0).map((need: any, i: number) => (
                  <li key={i}>
                    <strong>{need.tenant_count} tenant workshops</strong> need <strong>{need.clusters_needed} clusters</strong> ({need.capacity_per_cluster} tenants/cluster)
                    <br />
                    <span style={{ fontSize: '0.85rem', color: 'var(--pf-v6-global--Color--200)' }}>
                      CSV has {need.clusters_in_csv} cluster rows → need {need.deficit} more: <code>{need.cluster_ci}</code>
                    </span>
                  </li>
                ))}
              </ul>
              <div style={{ padding: '10px 14px', background: 'var(--pf-v6-global--BackgroundColor--200)', border: '1px solid var(--pf-v6-global--BorderColor--100)', borderRadius: 4 }}>
                <strong>⚠️ Action needed:</strong> Add {clusterNeeds.total_deficit} cluster CI row(s) to your CSV, or ensure clusters already exist in the pool.
                <br />
                <span style={{ fontSize: '0.85rem', marginTop: 4, display: 'block' }}>
                  Clusters take ~4 hours to provision. Schedule cluster rows at least 4 hours before their tenants.
                </span>
              </div>
            </Alert>
          )}

          {/* Tenant cluster readiness — preferred path is let Babylon manage the
              pool on provision (longer deploy). Direct-sandbox with no clusters
              still blocks; missing pool alone is a warning. */}
          {missingTenantRefs && (() => {
            const all = [
              ...(missingTenantRefs.missing_refs || []),
              ...(missingTenantRefs.ref_no_pool || []),
            ];
            const willFail = all.filter((r: any) => !r.pool_exists && !r.has_cluster_row);
            const willFailDirect = willFail.filter((r: any) => r.direct_sandbox);
            const willFailNoPool = willFail.filter((r: any) => !r.direct_sandbox);
            const viaFreshCluster = all.filter((r: any) => !r.pool_exists && r.has_cluster_row);
            const willFailDirectUnique = uniqueByCi(willFailDirect);
            const willFailNoPoolUnique = uniqueByCi(willFailNoPool);

            if (willFail.length === 0 && viaFreshCluster.length === 0) return null;

            const openPoolCreate = (refs: typeof willFail) => {
              setPoolCreateCIs(clusterCisFromTenantRefs(refs));
              setPoolCreateYaml('');
              setPoolCreateResults([]);
              setPoolCreateApplied(false);
              setPoolCreateTimeShifted(0);
              setShowPoolCreateModal(true);
            };

            const shiftTenantsEarlier4h = async (refs: typeof willFail) => {
              const tenantCIs = new Set(refs.map((r: any) => r.ci as string));
              const shifted = schedules.map((s) =>
                tenantCIs.has(s.ci)
                  ? {
                      ...s,
                      provisioning_date: shiftScheduleDate(s.provisioning_date, -4),
                      auto_stop: shiftScheduleDate(s.auto_stop, -4),
                      auto_destroy: shiftScheduleDate(s.auto_destroy, -4),
                    }
                  : s,
              );
              const shiftedCount = shifted.filter(
                (s, i) => s.provisioning_date !== schedules[i].provisioning_date,
              ).length;
              setSchedules(shifted);
              try { await api.updateSchedules(shifted); } catch { /* non-fatal */ }
              if (shiftedCount > 0) {
                await recordOperatorOverride({
                  action: 'shift_minus_4h',
                  summary: `−4h earlier on ${shiftedCount} workshop(s) vs Labagator/event`,
                  detail: 'Extra cluster hours before students (cost). Operator accepted schedule drift.',
                  affected_count: shiftedCount,
                });
              }
              showToast(
                shiftedCount > 0
                  ? `Schedule drift: ${shiftedCount} workshop(s) −4h earlier than Labagator/event — extra cluster hours before students (cost)`
                  : 'No start times changed',
                shiftedCount > 0 ? 'info' : 'danger',
              );
            };

            return (
              <>
                {willFailDirectUnique.length > 0 && (
                  <Alert
                    variant="danger"
                    isInline
                    title={
                      willFailDirect.length === willFailDirectUnique.length
                        ? `${willFailDirectUnique.length} catalog item(s) blocked — no clusters registered in sandbox-api`
                        : `${willFailDirectUnique.length} catalog item(s) blocked (${willFailDirect.length} schedule rows) — no clusters registered in sandbox-api`
                    }
                    style={{ marginBottom: 12 }}
                    actionLinks={
                      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                        <Button
                          variant="danger"
                          size="sm"
                          onClick={() => openPoolCreate(willFailDirectUnique)}
                        >
                          {`Option 2: Create ${clusterCisFromTenantRefs(willFailDirectUnique).length} TenantClusterPool${clusterCisFromTenantRefs(willFailDirectUnique).length === 1 ? '' : 's'}`}
                        </Button>
                        <Button
                          variant="link"
                          size="sm"
                          onClick={async () => {
                            try { setMissingTenantRefs(await api.checkTenantClusterRefs(targetCluster)); } catch { /* ignore */ }
                          }}
                        >
                          Re-check
                        </Button>
                      </div>
                    }
                  >
                    <div style={{ marginBottom: 8 }}>
                      These workshops use direct sandbox-api cluster assignment but have no TenantClusterPool with available clusters.
                      Two ways to fix this:
                    </div>
                    <div style={{ fontSize: '0.9rem', marginBottom: 4 }}>
                      <strong>Option 1 — Permanent fix (recommended):</strong> Add <code>tenant_cluster.item</code> to each
                      CI&apos;s <code>event.yaml</code> in agnosticv. Babylon will manage the cluster pool automatically going forward.
                    </div>
                    <ul style={{ margin: '0 0 10px 20px', fontSize: '0.85rem' }}>
                      {willFailDirectUnique.map((ref: any) => {
                        const parts = (ref.ci as string).split('.');
                        const agvPath = parts.slice(0, -1).join('/') + '/' + parts[parts.length - 1] + '.yaml';
                        const agvUrl = `https://github.com/rhpds/agnosticv/edit/master/${agvPath}`;
                        const rowCount = willFailDirect.filter((r: any) => r.ci === ref.ci).length;
                        return (
                          <li key={ref.ci}>
                            <strong>{ref.workshop_name}</strong>
                            {rowCount > 1 ? (
                              <span style={{ color: 'var(--pf-v6-global--Color--200)', fontSize: '0.8rem' }}>
                                {' '}({rowCount} schedule rows)
                              </span>
                            ) : null}
                            {' '}
                            <a href={agvUrl} target="_blank" rel="noreferrer" style={{ fontSize: '0.8rem' }}>
                              edit event.yaml in agnosticv ↗
                            </a>
                          </li>
                        );
                      })}
                    </ul>
                    <div style={{ fontSize: '0.9rem', marginBottom: 4 }}>
                      <strong>Option 2 — Quick fix for this event:</strong> Create TenantClusterPool(s) now and enable them.
                      Babylon will provision clusters automatically once the pool is enabled (allow ~4h before tenant start).
                    </div>
                    <div style={{ fontSize: '0.8rem', color: 'var(--pf-v6-global--Color--200)' }}>
                      Make sure to set <strong>Enable pool</strong> in the creation dialog so Babylon starts provisioning immediately.
                    </div>
                  </Alert>
                )}
                {willFailNoPoolUnique.length > 0 && (
                  <Alert
                    variant="warning"
                    isInline
                    title={
                      willFailNoPool.length === willFailNoPoolUnique.length
                        ? `${willFailNoPoolUnique.length} catalog item(s) — no TenantClusterPool yet (Babylon can create on deploy)`
                        : `${willFailNoPoolUnique.length} catalog item(s) (${willFailNoPool.length} schedule rows) — no TenantClusterPool yet (Babylon can create on deploy)`
                    }
                    style={{ marginBottom: 12 }}
                    actionLinks={
                      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                        <Button
                          variant="primary"
                          size="sm"
                          onClick={() => {
                            showToast(
                              'OK — deploy as usual; Babylon will manage TenantClusterPools (longer deploy)',
                              'info',
                            );
                          }}
                        >
                          Let Babylon handle it
                        </Button>
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() => openPoolCreate(willFailNoPoolUnique)}
                        >
                          {`Optional: pre-create ${clusterCisFromTenantRefs(willFailNoPoolUnique).length} pool${clusterCisFromTenantRefs(willFailNoPoolUnique).length === 1 ? '' : 's'}`}
                        </Button>
                        <Button
                          variant="tertiary"
                          size="sm"
                          onClick={() => shiftTenantsEarlier4h(willFailNoPoolUnique)}
                        >
                          Optional: −4h earlier (cost)
                        </Button>
                        <Button
                          variant="link"
                          size="sm"
                          onClick={async () => {
                            try { setMissingTenantRefs(await api.checkTenantClusterRefs(targetCluster)); } catch { /* ignore */ }
                          }}
                        >
                          Re-check
                        </Button>
                      </div>
                    }
                  >
                    <div style={{ marginBottom: 8 }}>
                      <strong>Default:</strong> Deploy anyway and let Babylon create/manage the
                      {' '}<code>TenantClusterPool</code> when tenants provision. This is safe to ignore —
                      expect a <strong>longer deploy</strong> while clusters fill (often 30–60+ min).
                    </div>
                    <AffectsItemsList
                      count={willFailNoPoolUnique.length}
                      expanded={tenantNoPoolExpanded}
                      onToggle={() => setTenantNoPoolExpanded((v) => !v)}
                      noun="catalog item"
                    >
                      {sortByCiName(willFailNoPoolUnique).map((ref: any) => {
                        const rowCount = willFailNoPool.filter((r: any) => r.ci === ref.ci).length;
                        return (
                          <li key={ref.ci}>
                            <strong>{ref.workshop_name}</strong>
                            {rowCount > 1 ? (
                              <span style={{ color: 'var(--pf-v6-global--Color--200)', fontSize: '0.8rem' }}>
                                {' '}({rowCount} schedule rows)
                              </span>
                            ) : null}
                          </li>
                        );
                      })}
                    </AffectsItemsList>
                    <div style={{ fontSize: '0.85rem', color: 'var(--pf-v6-global--Color--200)' }}>
                      Optional only (not applied automatically):{' '}
                      <strong>pre-create pools</strong> or <strong>−4h earlier</strong> starts capacity before
                      Labagator/event times — you pay for idle cluster hours until students arrive; or add matching
                      {' '}<code>-cluster.*</code> CI rows to the CSV.
                    </div>
                  </Alert>
                )}
                {viaFreshCluster.length > 0 && (
                  <Alert
                    variant="info"
                    isInline
                    title={`${viaFreshCluster.length} workshop(s) covered by cluster CI in this CSV — OK`}
                    style={{ marginBottom: 12 }}
                  >
                    <div style={{ marginBottom: 6 }}>
                      No shared pool exists yet, but each of these tenant workshops has a matching
                      <code> -cluster.*</code> CI row in this CSV and the same namespace.
                      Babylon will route the tenant onto that dedicated cluster.
                    </div>
                    <AffectsItemsList
                      count={viaFreshCluster.length}
                      expanded={viaFreshExpanded}
                      onToggle={() => setViaFreshExpanded((v) => !v)}
                      noun="workshop"
                    >
                      {sortByCiName(viaFreshCluster).map((ref: any, i: number) => (
                        <li key={i}><strong>{ref.workshop_name}</strong></li>
                      ))}
                    </AffectsItemsList>
                    <div style={{ fontSize: '0.8rem', color: 'var(--pf-v6-global--Color--200)' }}>
                      Dedicated clusters provision from scratch (~2–4 h). A <code>TenantClusterPool</code> is faster
                      for future events since Babylon keeps clusters warm in advance.
                    </div>
                  </Alert>
                )}
              </>
            );
          })()}

          {/* Multi-asset password info */}
          {needsPasswordWarning && (
            <Alert variant="info" isInline title="Multi-asset passwords (optional)" style={{ marginBottom: 12 }}>
              Multi-asset workshop(s) detected. If each asset CI needs its own password, upload a passwords CSV above.
              Otherwise, the main CSV password will be used for all assets.
            </Alert>
          )}

          <Alert variant="info" isInline isPlain title="⏰ Schedule times are in UTC" style={{ marginBottom: 8 }}>
            {(() => {
              const offsetMins = new Date().getTimezoneOffset();
              // getTimezoneOffset() is positive for west of UTC, negative for east.
              // BST = -60 → UTC+1. Flip the sign so display matches convention.
              const sign = offsetMins <= 0 ? '+' : '-';
              const absH = Math.floor(Math.abs(offsetMins) / 60);
              const absM = Math.abs(offsetMins) % 60;
              const offsetStr = offsetMins === 0
                ? ''
                : absM > 0
                  ? `${sign}${absH}:${String(absM).padStart(2, '0')}`
                  : `${sign}${absH}`;
              const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
              return (
                <>
                  Your local timezone: <strong>{tz}</strong> (UTC{offsetStr}).{' '}
                  Enter dates in <strong>DD/MM/YYYY HH:MM</strong> format as <strong>UTC</strong> — not your local time.
                  Example: if your event starts at 15:00 UTC, enter <code>14/09/2026 15:00</code>.
                </>
              );
            })()}
          </Alert>

          <div className="table-sticky-wrapper">
            <Table aria-label="Schedule preview" variant="compact" className="fixed-table" isStickyHeader>
              <Thead>
                <Tr>
                  <Th />
                  <Th>Item Type</Th>
                  <Th>CI Name</Th>
                  <Th>CI (Catalog Item)</Th>
                  <Th>Cluster</Th>
                  {usePoolLookup && (
                    <Th>
                      Resource Pool{' '}
                      <Tooltip
                        content={
                          <div>
                            Shows matched pool status + dropdown to override:<br />
                            • Ready: Resources available now<br />
                            • Provisioning: Resources being created<br />
                            • Use dropdown to pick any cluster pool<br />
                            • Select "(keep current)" to use catalog item
                          </div>
                        }
                      >
                        <InfoCircleIcon style={{ color: 'var(--pf-v6-global--info-color--100)', cursor: 'help' }} />
                      </Tooltip>
                    </Th>
                  )}
                  <Th>Workshop Name</Th>
                  <Th>Namespace</Th>
                  <Th>
                    Catalog Namespace{' '}
                    <Tooltip
                      content={
                        <div>
                          Auto-detected from CI suffix:<br />
                          • .event → babylon-catalog-event<br />
                          • .prod → babylon-catalog-prod<br />
                          • .dev → babylon-catalog-dev<br />
                          • (no suffix) → babylon-catalog-prod (default)<br />
                          Override via CSV Catalog_Namespace column.
                        </div>
                      }
                    >
                      <InfoCircleIcon style={{ color: 'var(--pf-v6-global--info-color--100)', cursor: 'help' }} />
                    </Tooltip>
                  </Th>
                  <Th>Users</Th>
                  <Th>Instances</Th>
                  <Th>UI</Th>
                  <Th>Redirect</Th>
                  <Th>
                    <Tooltip content="Workshop password. Leave blank to auto-generate on deploy.">
                      <span>Password</span>
                    </Tooltip>
                  </Th>
                  <Th>Prov. Date (UTC)</Th>
                  <Th>Auto-Stop (UTC)</Th>
                  <Th>Auto-Destroy (UTC)</Th>
                  <Th>Actions</Th>
                </Tr>
              </Thead>
              <Tbody>
                {filteredSchedules.map(({ s, i }) => {
                  const rowStyle: React.CSSProperties = {};
                  if (s.item_type === 'Cluster') {
                    rowStyle.backgroundColor = 'rgba(0, 102, 204, 0.1)'; // blue tint
                  } else if (s.item_type === 'Tenant') {
                    rowStyle.backgroundColor = 'rgba(0, 204, 102, 0.1)'; // green tint
                  }
                  if (s.auto_added) {
                    // Flow-injected cluster — make it obvious and distinct.
                    rowStyle.backgroundColor = 'rgba(62, 134, 53, 0.12)';
                    rowStyle.borderLeft = '3px solid #3e8635';
                  }

                  return (
                  <Fragment key={`${s.ci}-${s.namespace}-${i}`}>
                    <Tr className={warningRowIndices.has(i) ? 'warning-row' : undefined} style={rowStyle}>
                      <Td
                        expand={{
                          rowIndex: i,
                          isExpanded: expandedRows.has(i),
                          onToggle: () => toggleExpanded(i),
                        }}
                      />
                      <Td dataLabel="Item Type">
                        <FormSelect
                          value={s.item_type || 'Workshop'}
                          onChange={(_e, value) => {
                            const updated = schedules.map((sc, idx) => idx === i ? { ...sc, item_type: value as 'Workshop' | 'Cluster' | 'Tenant' } : sc);
                            setSchedules(updated);
                            api.updateSchedules(updated).catch(err => showToast(`Failed to update schedule: ${err}`, 'danger'));
                          }}
                          aria-label={`Item type for ${s.ci_name}`}
                          style={{ minWidth: '120px' }}
                        >
                          <FormSelectOption key="workshop" value="Workshop" label="Workshop" />
                          <FormSelectOption key="cluster" value="Cluster" label="Cluster" />
                          <FormSelectOption key="tenant" value="Tenant" label="Tenant" />
                        </FormSelect>
                      </Td>
                      <Td dataLabel="CI Name">
                        {s.auto_added && (
                          <span style={{
                            display: 'inline-block', marginBottom: 4, padding: '1px 8px',
                            fontSize: '0.7rem', fontWeight: 600, color: '#fff',
                            background: '#3e8635', borderRadius: 10,
                          }}>
                            added by Flow
                          </span>
                        )}
                        <TextInput
                          value={s.ci_name || ''}
                          onChange={(_e, value) => {
                            const updated = schedules.map((sc, idx) => idx === i ? { ...sc, ci_name: value } : sc);
                            setSchedules(updated);
                            api.updateSchedules(updated).catch(err => showToast(`Failed to update schedule: ${err}`, 'danger'));
                          }}
                          placeholder="CI display name"
                          style={{ minWidth: '150px' }}
                          readOnly={rowEditsLocked}
                        />
                      </Td>
                      <Td dataLabel="CI" style={{ minWidth: '280px' }}>
                        {useCatalogLookup ? (
                          <div style={{ width: '100%' }}>
                            <CatalogItemSelect
                              value={s.ci || ''}
                              onChange={(value) => {
                                const updated = schedules.map((sc, idx) => idx === i ? { ...sc, ci: value } : sc);
                                setSchedules(updated);
                                api.updateSchedules(updated).catch(err => showToast(`Failed to update schedule: ${err}`, 'danger'));
                              }}
                              label=""
                              helperText=""
                              filterNamespace={s.catalog_namespace}
                            />
                          </div>
                        ) : (
                          <TextInput
                            value={s.ci || ''}
                            onChange={(_e, value) => {
                              const updated = schedules.map((sc, idx) => idx === i ? { ...sc, ci: value } : sc);
                              setSchedules(updated);
                              api.updateSchedules(updated).catch(err => showToast(`Failed to update schedule: ${err}`, 'danger'));
                            }}
                            placeholder="vendor.item.env"
                            style={{ minWidth: '200px' }}
                            readOnly={rowEditsLocked}
                          />
                        )}
                      </Td>
                      <Td dataLabel="Cluster">
                        {s.item_type === 'Tenant' && s.detected_cluster_ci ? (
                          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                            <span>{s.detected_cluster_ci}</span>
                            {s.cluster_ci_source && (
                              <Label
                                isCompact
                                color={
                                  s.cluster_ci_source === 'agnosticv'
                                    ? 'green'
                                    : s.cluster_ci_source === 'override'
                                    ? 'blue'
                                    : 'grey'
                                }
                              >
                                {s.cluster_ci_source === 'agnosticv'
                                  ? 'AgnosticV'
                                  : s.cluster_ci_source === 'override'
                                  ? 'Manual override'
                                  : 'Naming convention'}
                              </Label>
                            )}
                          </div>
                        ) : (
                          <span style={{ color: 'var(--pf-v6-global--Color--200)' }}>-</span>
                        )}
                      </Td>
                      {usePoolLookup && (
                        <Td dataLabel="Resource Pool" style={{ minWidth: '280px' }}>
                          {poolLookupData[s.ci] ? (
                            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                              {poolLookupData[s.ci].has_pool && poolLookupData[s.ci].pool && (
                                <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                                  <div style={{ fontSize: '0.75rem', color: 'var(--pf-v6-global--Color--200)' }}>
                                    <strong>{poolLookupData[s.ci].pool!.pool_name}</strong>
                                  </div>
                                  <div style={{ fontSize: '0.7rem', color: 'var(--pf-v6-global--Color--300)' }}>
                                    Ready: {poolLookupData[s.ci].pool!.ready} / Min: {poolLookupData[s.ci].pool!.min_available}
                                    {poolLookupData[s.ci].pool!.provisioning > 0 && (
                                      <span style={{ color: 'var(--pf-v6-global--warning-color--100)' }}>
                                        {' '}| Provisioning: {poolLookupData[s.ci].pool!.provisioning}
                                      </span>
                                    )}
                                  </div>
                                </div>
                              )}
                              {allPools.length > 0 && (
                                <>
                                  <SearchInput
                                    placeholder="Filter pools..."
                                    value={poolSearchFilters[i] || ''}
                                    onChange={(_e, value) => {
                                      setPoolSearchFilters(prev => ({ ...prev, [i]: value }));
                                    }}
                                    onClear={() => {
                                      setPoolSearchFilters(prev => {
                                        const updated = { ...prev };
                                        delete updated[i];
                                        return updated;
                                      });
                                    }}
                                    style={{ marginBottom: '4px', fontSize: '0.8rem' }}
                                  />
                                  <FormSelect
                                    value={s.ci || ''}
                                    onChange={(_e, value) => {
                                      const updated = schedules.map((sc, idx) => idx === i ? { ...sc, ci: value as string } : sc);
                                      setSchedules(updated);
                                      api.updateSchedules(updated).catch(err => showToast(`Failed to update schedule: ${err}`, 'danger'));
                                    }}
                                    aria-label={`Override pool for ${s.ci_name}`}
                                    className="pool-override-select"
                                  >
                                    <FormSelectOption key="use-catalog" value={s.ci || ''} label="(keep current)" />
                                    {allPools
                                      .filter(pool => {
                                        const searchTerm = (poolSearchFilters[i] || '').toLowerCase();
                                        if (!searchTerm) return true;
                                        return pool.pool_name.toLowerCase().includes(searchTerm);
                                      })
                                      .map(pool => (
                                        <FormSelectOption
                                          key={pool.pool_name}
                                          value={pool.pool_name}
                                          label={`${pool.pool_name} (Ready: ${pool.ready})`}
                                        />
                                      ))}
                                  </FormSelect>
                                </>
                              )}
                            </div>
                          ) : (
                            <Spinner size="md" />
                          )}
                        </Td>
                      )}
                      <Td dataLabel="Workshop Name">
                        <TextInput
                          value={s.workshop_name || ''}
                          onChange={(_e, value) => {
                            const updated = schedules.map((sc, idx) => idx === i ? { ...sc, workshop_name: value } : sc);
                            setSchedules(updated);
                            api.updateSchedules(updated).catch(err => showToast(`Failed to update schedule: ${err}`, 'danger'));
                          }}
                          placeholder="Workshop name"
                          style={{ minWidth: '120px' }}
                          readOnly={rowEditsLocked}
                        />
                      </Td>
                      <Td dataLabel="Namespace">
                        <TextInput
                          value={s.namespace || ''}
                          onChange={(_e, value) => {
                            const updated = schedules.map((sc, idx) => idx === i ? { ...sc, namespace: value } : sc);
                            setSchedules(updated);
                            api.updateSchedules(updated).catch(err => showToast(`Failed to update schedule: ${err}`, 'danger'));
                          }}
                          placeholder="user-example-redhat-com"
                          style={{ minWidth: '180px' }}
                          readOnly={rowEditsLocked}
                        />
                      </Td>
                      <Td dataLabel="Catalog Namespace">
                        <FormSelect
                          value={s.catalog_namespace || ''}
                          onChange={(_e, value) => {
                            const updated = schedules.map((sc, idx) => idx === i ? { ...sc, catalog_namespace: value } : sc);
                            setSchedules(updated);
                            api.updateSchedules(updated).catch(err => showToast(`Failed to update schedule: ${err}`, 'danger'));
                          }}
                          aria-label="Catalog namespace"
                          style={{ minWidth: '180px' }}
                          isDisabled={rowEditsLocked}
                        >
                          <FormSelectOption value="" label="Auto-detect" />
                          <FormSelectOption value="babylon-catalog-event" label="babylon-catalog-event" />
                          <FormSelectOption value="babylon-catalog-prod" label="babylon-catalog-prod" />
                          <FormSelectOption value="babylon-catalog-dev" label="babylon-catalog-dev" />
                        </FormSelect>
                      </Td>
                      <Td dataLabel="Users">
                        <TextInput
                          value={s.users?.toString() || ''}
                          onChange={(_e, value) => {
                            const updated = schedules.map((sc, idx) => idx === i ? { ...sc, users: value ? parseInt(value) : null } : sc);
                            setSchedules(updated);
                            api.updateSchedules(updated).catch(err => showToast(`Failed to update schedule: ${err}`, 'danger'));
                          }}
                          type="number"
                          style={{ width: '90px' }}
                          readOnly={rowEditsLocked}
                        />
                      </Td>
                      <Td dataLabel="Instances">
                        <TextInput
                          value={s.instances?.toString() || ''}
                          onChange={(_e, value) => {
                            const updated = schedules.map((sc, idx) => idx === i ? { ...sc, instances: value ? parseInt(value) : null } : sc);
                            setSchedules(updated);
                            api.updateSchedules(updated).catch(err => showToast(`Failed to update schedule: ${err}`, 'danger'));
                          }}
                          type="number"
                          style={{ width: '90px' }}
                          readOnly={rowEditsLocked}
                        />
                      </Td>
                      <Td dataLabel="UI">{s.enable_workshop_interface ? 'Yes' : 'No'}</Td>
                      <Td dataLabel="Redirect">
                        <Switch
                          id={`redirect-row-${i}`}
                          aria-label={`Redirect ${s.ci_name}`}
                          isChecked={s.redirect}
                          onChange={() => {
                            setSchedules(schedules.map((sc, idx) => idx === i ? { ...sc, redirect: !sc.redirect } : sc));
                          }}
                          isReversed
                          isDisabled={rowEditsLocked}
                        />
                      </Td>
                      <Td dataLabel="Password">
                        <Split hasGutter style={{ alignItems: 'center' }}>
                          <SplitItem isFilled>
                            <TextInput
                              id={`password-${i}`}
                              value={s.password || ''}
                              onChange={(_e, value) => {
                                const updated = schedules.map((sc, idx) => idx === i ? { ...sc, password: value } : sc);
                                setSchedules(updated);
                                api.updateSchedules(updated).catch(err => showToast(`Failed to update schedule: ${err}`, 'danger'));
                              }}
                              placeholder="auto"
                              style={{ minWidth: '100px' }}
                              readOnly={rowEditsLocked}
                              type="text"
                            />
                          </SplitItem>
                          <SplitItem>
                            <Button
                              variant="plain"
                              aria-label="Generate password"
                              onClick={() => {
                                const newPass = Math.random().toString(36).slice(-8);
                                const updated = schedules.map((sc, idx) => idx === i ? { ...sc, password: newPass } : sc);
                                setSchedules(updated);
                                api.updateSchedules(updated).catch(err => showToast(`Failed to update schedule: ${err}`, 'danger'));
                                showToast('Password generated', 'success');
                              }}
                              isDisabled={rowEditsLocked}
                            >
                              🎲
                            </Button>
                          </SplitItem>
                        </Split>
                      </Td>
                      <Td dataLabel="Prov. Date (UTC)" className="date-cell">
                        <TextInput
                          id={`prov-date-${i}`}
                          value={s.provisioning_date || ''}
                          onChange={(_e, value) => {
                            const updated = schedules.map((sc, idx) => idx === i ? { ...sc, provisioning_date: value } : sc);
                            setSchedules(updated);
                            // Update backend
                            api.updateSchedules(updated).catch(err => showToast(`Failed to update schedule: ${err}`, 'danger'));
                          }}
                          placeholder="DD/MM/YYYY HH:MM"
                          style={{ minWidth: '140px' }}
                          readOnly={rowEditsLocked}
                        />
                      </Td>
                      <Td dataLabel="Auto-Stop (UTC)" className="date-cell">
                        <TextInput
                          id={`auto-stop-${i}`}
                          value={s.auto_stop || ''}
                          onChange={(_e, value) => {
                            const updated = schedules.map((sc, idx) => idx === i ? { ...sc, auto_stop: value } : sc);
                            setSchedules(updated);
                            // Update backend
                            api.updateSchedules(updated).catch(err => showToast(`Failed to update schedule: ${err}`, 'danger'));
                          }}
                          placeholder="DD/MM/YYYY HH:MM"
                          style={{ minWidth: '140px' }}
                          readOnly={rowEditsLocked}
                        />
                      </Td>
                      <Td dataLabel="Auto-Destroy (UTC)" className="date-cell">
                        <TextInput
                          id={`auto-destroy-${i}`}
                          value={s.auto_destroy || ''}
                          onChange={(_e, value) => {
                            const updated = schedules.map((sc, idx) => idx === i ? { ...sc, auto_destroy: value } : sc);
                            setSchedules(updated);
                            // Update backend
                            api.updateSchedules(updated).catch(err => showToast(`Failed to update schedule: ${err}`, 'danger'));
                          }}
                          placeholder="DD/MM/YYYY HH:MM"
                          style={{ minWidth: '140px' }}
                          readOnly={rowEditsLocked}
                        />
                      </Td>
                      <Td dataLabel="Actions">
                        <Tooltip content="Delete this schedule">
                          <Button
                            variant="plain"
                            aria-label={`Delete ${s.ci_name}`}
                            isDisabled={rowEditsLocked}
                            onClick={async () => {
                              try {
                                await api.deleteSchedule(i);
                                const newSchedules = schedules.filter((_, idx) => idx !== i);
                                setSchedules(newSchedules);
                                showToast(`Deleted ${s.ci_name}`, 'info');
                              } catch (err) {
                                showToast(`Failed to delete schedule: ${err}`, 'danger');
                              }
                            }}
                          >
                            <TrashIcon />
                          </Button>
                        </Tooltip>
                      </Td>
                    </Tr>
                    {expandedRows.has(i) && (
                      <Tr key={`detail-${i}`} isExpanded>
                        <Td colSpan={columnCount + 1}>
                          <ExpandableRowContent>
                            <div className="schedule-detail-grid">
                              <div><strong>Password:</strong> {s.password || '-'}</div>
                              <div><strong>Activity:</strong> {s.activity || '-'}</div>
                              <div><strong>Purpose:</strong> {s.purpose || '-'}</div>
                              <div><strong>Salesforce IDs:</strong> {s.salesforce_ids || '-'}</div>
                              <div><strong>Concurrency:</strong> {s.concurrency ?? '-'}</div>
                              <div><strong>Multi-Asset:</strong> {s.is_multi_asset ? 'Yes' : 'No'}</div>
                              {s.is_multi_asset && (
                                <>
                                  <div><strong>Asset CIs:</strong> {s.asset_cis || '-'}</div>
                                  <div><strong>Multi Workshop Name:</strong> {s.multi_workshop_name || '-'}</div>
                                </>
                              )}
                              {s.aws_regions && (() => {
                                const regions = s.aws_regions.split(',').map(r => r.trim()).filter(Boolean);
                                const total = s.users || 0;
                                const base = regions.length > 1 ? Math.floor(total / regions.length) : total;
                                const rem = regions.length > 1 ? total % regions.length : 0;
                                return (
                                  <div style={{ gridColumn: '1 / -1' }}>
                                    <strong>AWS Regions:</strong>{' '}
                                    {regions.map((r, idx) => {
                                      const count = base + (idx < rem ? 1 : 0);
                                      return <span key={r} style={{ marginRight: 12 }}>{r} ({count} users)</span>;
                                    })}
                                    {regions.length >= 2 && <em style={{ fontSize: '0.85em', opacity: 0.7 }}> — multi-region deploy</em>}
                                  </div>
                                );
                              })()}
                            </div>
                          </ExpandableRowContent>
                        </Td>
                      </Tr>
                    )}
                  </Fragment>
                );
                })}
              </Tbody>
            </Table>
          </div>

          {/* Deploy settings */}
          <Card isCompact style={{ marginBottom: 16 }}>
            <CardTitle>Deploy Settings</CardTitle>
            <CardBody>
              <Split hasGutter>
                <SplitItem>
                  <Tooltip content="Prevents non-admin users from modifying resource settings in the RHDP UI. Sets the demo.redhat.com/lock-enabled label.">
                    <Switch
                      id="resource-lock-switch"
                      label="Lock UI Admin Settings"
                      isChecked={resourceLock}
                      onChange={(_e, checked) => setResourceLock(checked)}
                    />
                  </Tooltip>
                </SplitItem>
                <SplitItem>
                  <Tooltip content="Enable Poolboy resource pool allocation for flexible resource sharing across workshops. Leave off for dedicated per-workshop resources.">
                    <Switch
                      id="resource-pools-switch"
                      label="Enable Resource Pools"
                      isChecked={enableResourcePools}
                      onChange={(_e, checked) => {
                        setEnableResourcePools(checked);
                        if (!checked) setUsePoolLookup(false); // Turn off pool lookup if resource pools disabled
                      }}
                    />
                  </Tooltip>
                </SplitItem>
                {enableResourcePools && (
                  <>
                    <SplitItem>
                      <Tooltip content="Query the cluster for available resource pools for each catalog item. Shows pool status (ready count, provisioning, etc.) and allows overriding which pool to use. Leave off to skip pool validation during upload.">
                        <Switch
                          id="pool-lookup-switch"
                          label="Pool Lookup"
                          isChecked={usePoolLookup}
                          onChange={(_e, checked) => setUsePoolLookup(checked)}
                        />
                      </Tooltip>
                    </SplitItem>
                  </>
                )}
                <SplitItem>
                  <Tooltip content="Mark workshops as fully managed and pre-configured. Applies the white-glove label for managed delivery.">
                    <Switch
                      id="white-glove-switch"
                      label="White Glove"
                      isChecked={whiteGlove}
                      onChange={(_e, checked) => setWhiteGlove(checked)}
                    />
                  </Tooltip>
                </SplitItem>
                <SplitItem>
                  <Tooltip content="Automatically redirect students to the lab UI after they log in to the workshop. Toggles all rows; override individual rows in the table.">
                    <Switch
                      id="redirect-switch"
                      label="Redirect (all)"
                      isChecked={redirect}
                      onChange={(_e, checked) => {
                        setRedirect(checked);
                        if (schedules.length > 0) {
                          setSchedules(schedules.map(s => ({ ...s, redirect: checked })));
                        }
                      }}
                    />
                  </Tooltip>
                </SplitItem>
                <SplitItem>
                  <Tooltip content="Enable catalog dropdown with search/filter for CI field. When off, uses plain text input for faster loading (default).">
                    <Switch
                      id="catalog-lookup-switch"
                      label="Use Catalog Lookup"
                      isChecked={useCatalogLookup}
                      onChange={(_e, checked) => setUseCatalogLookup(checked)}
                    />
                  </Tooltip>
                </SplitItem>
                <SplitItem>
                  <Tooltip content="Pause between Workshop/WorkshopProvision creates (API scheduling only — not VM provision). Auto: 0s (<10), 0.5s (10–24), 1s (25+).">
                    <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span style={{ fontSize: '0.875rem', whiteSpace: 'nowrap' }}>Deploy pace</span>
                      <FormSelect
                        id="deploy-pace-select"
                        aria-label="Deploy pace between workshops"
                        value={deployDelaySeconds}
                        onChange={(_e, value) => setDeployDelaySeconds(value)}
                        style={{ width: 'auto', minWidth: 140 }}
                      >
                        <FormSelectOption value="" label="Auto (by batch size)" />
                        <FormSelectOption value="0" label="0s (no pause)" />
                        <FormSelectOption value="0.5" label="0.5s" />
                        <FormSelectOption value="1" label="1s" />
                        <FormSelectOption value="3" label="3s (gentle)" />
                        <FormSelectOption value="5" label="5s" />
                      </FormSelect>
                    </span>
                  </Tooltip>
                </SplitItem>
                {deployClusters.length > 0 && (
                  <SplitItem>
                    <Tooltip content="Physical OpenShift cluster for this deploy. Defaults to Events (us-west-2) — not infra01 where Flow runs. Infra01 is only for the Flow app itself.">
                      <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <span style={{ fontSize: '0.875rem', whiteSpace: 'nowrap' }}>Deploy to</span>
                        <FormSelect
                          id="target-cluster-select"
                          aria-label="Deploy target cluster"
                          value={targetCluster}
                          onChange={(_e, value) => {
                            selectTargetCluster(value);
                            setTargetCluster(value);
                            setMissingNamespaces([]);
                            setNumUsersViolations([]);
                            setUsersNotInCatalog([]);
                            setUsersBlankCatalogDefault([]);
                            setNumUsersLimits({});
                            setCatalogNamespaceMismatches([]);
                            setProdNotEvent([]);
                            setCatalogSuffixCorrections([]);
                            setCatalogNotFound([]);
                            setSkippedCatalogSummary(null);
                            setPoolCapacityWarnings([]);
                            setPoolsNotFound([]);
                            setMissingTenantRefs(null);
                            setClusterNeeds(null);
                            setPoolLookupData({});
                            setAllPools([]);
                            setShowPoolCreateModal(false);
                            const label = value === ''
                              ? 'This cluster (infra01)'
                              : (deployClusters.find((c) => c.key === value)?.display_name || value);
                            showToast(`Deploy target: ${label}. Check prerequisites before deploying.`, 'info');
                          }}
                          style={{ width: 'auto', minWidth: 180 }}
                        >
                          <FormSelectOption value="" label="This cluster (infra01) — Flow host only" />
                          {deployClusters.map((c) => (
                            <FormSelectOption
                              key={c.key}
                              value={c.key}
                              label={c.key === DEFAULT_TARGET_CLUSTER ? `${c.display_name} (default)` : c.display_name}
                            />
                          ))}
                        </FormSelect>
                      </span>
                    </Tooltip>
                  </SplitItem>
                )}
                {deployClusters.length > 0 && targetCluster && (
                  <SplitItem style={{ fontSize: '0.8rem', color: 'var(--pf-t--global--text--color--subtle)', alignSelf: 'center' }}>
                    → {deployClusters.find((c) => c.key === targetCluster)?.display_name || targetCluster}
                    {targetCluster === DEFAULT_TARGET_CLUSTER ? ' (event workshops)' : ''}
                  </SplitItem>
                )}
              </Split>
              {schedules.some(s => s.showroom_repo) && (
                <Split hasGutter style={{ marginTop: 8 }}>
                  <SplitItem style={{ fontWeight: 600, fontSize: '0.85rem', alignSelf: 'center' }}>Showroom:</SplitItem>
                  <SplitItem>
                    <Tooltip content="Enable noVNC remote desktop tab in Showroom for Windows-based or graphical workshops.">
                      <Switch
                        id="showroom-novnc-switch"
                        label="noVNC Desktop"
                        isChecked={showroomNovnc}
                        onChange={(_e, checked) => setShowroomNovnc(checked)}
                      />
                    </Tooltip>
                  </SplitItem>
                  <SplitItem>
                    <Tooltip content="Use the zerotouch chart variant with setup and runtime automation containers for fully hands-off provisioning.">
                      <Switch
                        id="showroom-zerotouch-switch"
                        label="Zerotouch Automation"
                        isChecked={showroomZerotouch}
                        onChange={(_e, checked) => setShowroomZerotouch(checked)}
                      />
                    </Tooltip>
                  </SplitItem>
                </Split>
              )}

              {/* Catalog Namespace Bulk Override */}
              {schedules.length > 0 && (
                <>
                  <div style={{ borderTop: '1px solid var(--pf-v6-global--BorderColor--100)', marginTop: 16, paddingTop: 16 }}>
                    <div style={{ fontWeight: 600, fontSize: '0.875rem', marginBottom: 8 }}>
                      Catalog Namespace
                    </div>
                    <div style={{ fontSize: '0.85rem', color: 'var(--pf-v6-global--Color--200)', marginBottom: 12 }}>
                      <InfoCircleIcon style={{ marginRight: 4 }} />
                      Auto-detected from CI suffix (.event → event, .prod → prod, .dev → dev, none → prod).
                      Override for all workshops if needed.
                    </div>
                    {(() => {
                      const detectionSummary = schedules.reduce((acc, s) => {
                        const detected = s.ci.endsWith('.event') ? 'babylon-catalog-event' :
                                       s.ci.endsWith('.prod') ? 'babylon-catalog-prod' :
                                       s.ci.endsWith('.dev') ? 'babylon-catalog-dev' :
                                       'babylon-catalog-prod';
                        acc[detected] = (acc[detected] || 0) + 1;
                        return acc;
                      }, {} as Record<string, number>);

                      return (
                        <div style={{ fontSize: '0.85rem', marginBottom: 12 }}>
                          <strong>Current detection:</strong>
                          <ul style={{ marginTop: 4, marginBottom: 0, paddingLeft: 20 }}>
                            {Object.entries(detectionSummary).map(([ns, count]) => (
                              <li key={ns}>{count} workshop{count > 1 ? 's' : ''} → {ns}</li>
                            ))}
                          </ul>
                        </div>
                      );
                    })()}
                    <div style={{ fontSize: '0.875rem', fontWeight: 600, marginBottom: 8 }}>
                      Override for all workshops:
                    </div>
                    <Split hasGutter>
                      <SplitItem>
                        <Button
                          variant="secondary"
                          onClick={() => handleCatalogOverride('event')}
                          size="sm"
                        >
                          Force Event Catalog
                        </Button>
                      </SplitItem>
                      <SplitItem>
                        <Button
                          variant="secondary"
                          onClick={() => handleCatalogOverride('prod')}
                          size="sm"
                        >
                          Force Prod Catalog
                        </Button>
                      </SplitItem>
                      <SplitItem>
                        <Button
                          variant="secondary"
                          onClick={() => handleCatalogOverride('dev')}
                          size="sm"
                        >
                          Force Dev Catalog
                        </Button>
                      </SplitItem>
                      <SplitItem>
                        <Button
                          variant="tertiary"
                          onClick={() => handleCatalogOverride('clear')}
                          size="sm"
                        >
                          Clear Overrides (Auto-detect)
                        </Button>
                      </SplitItem>
                    </Split>
                  </div>
                </>
              )}
            </CardBody>
          </Card>

          {/* Deploy buttons */}
          <Split hasGutter style={{ marginBottom: 16, flexWrap: 'wrap' }}>
            <SplitItem>
              <Tooltip content="Check namespaces, catalog items, user limits, and pool capacity. No resources are created.">
                <Button variant="secondary" onClick={handleValidate} isDisabled={deploying || validating || yamlDownloading}>
                  {validating ? 'Checking…' : 'Check prerequisites'}
                </Button>
              </Tooltip>
            </SplitItem>
            <SplitItem>
              <Tooltip content="Simulate deploy and update results preview; no resources created.">
                <Button variant="secondary" onClick={handleDryRun} isDisabled={deploying || validating || yamlDownloading}>
                  Preview deployment
                </Button>
              </Tooltip>
            </SplitItem>
            <SplitItem>
              <Tooltip content="Run dry-run and download ResourceClaim / Workshop / WorkshopProvision YAML (combined file).">
                <Button variant="secondary" onClick={handleDownloadYaml} isDisabled={deploying || validating || yamlDownloading}>
                  {yamlDownloading ? 'Preparing YAML…' : 'Download YAML'}
                </Button>
              </Tooltip>
            </SplitItem>
            <SplitItem>
              <Tooltip content="Download the current schedule table as CSV (confirmation copy — does not change deploy).">
                <Button variant="secondary" onClick={exportScheduleCsv} isDisabled={schedules.length === 0 || deploying}>
                  Export schedule CSV
                </Button>
              </Tooltip>
            </SplitItem>
            <SplitItem>
              <Tooltip content="Download deploy results (GUID, URL, status, errors) after a run.">
                <Button variant="secondary" onClick={exportDeployResultsCsv} isDisabled={!results?.length || deploying}>
                  Export results CSV
                </Button>
              </Tooltip>
            </SplitItem>
            <SplitItem>
              <Tooltip
                content={deployBlocked ? 'Fix blocking issues above before deploying' : ''}
                trigger={deployBlocked ? 'mouseenter focus' : 'manual'}
              >
                <Button variant="primary" onClick={handleDeploy} isDisabled={deploying || validating || yamlDownloading || deployBlocked} isDanger={!dryRun}>
                  {dryRun ? 'Run dry-run' : deployBlocked ? 'Deploy (blocked)' : 'Deploy'}
                </Button>
              </Tooltip>
            </SplitItem>
          </Split>
          <HelperText>
            <HelperTextItem>
              Check prerequisites checks cluster requirements. Preview deployment simulates deployment without creating resources.
              Both require the RHDP-Flow backend.
            </HelperTextItem>
            <HelperTextItem>
              {dryRun
                ? 'Dry-Run Mode is on: Run dry-run runs the deployment job with progress and logs, without provisioning resources.'
                : 'Dry-Run Mode is off: Deploy provisions real resources. Preview deployment remains a simulation.'}
            </HelperTextItem>
          </HelperText>

          {/* Diff view */}
          <DiffView hasSchedules={schedules.length > 0} showToast={showToast} />
        </>
      ) : (
        <EmptyState titleText="No schedules loaded" headingLevel="h3" icon={UploadIcon}>
          <EmptyStateBody>
            Upload a CSV file to preview and deploy workshop schedules.
            {scheduleExamples.length > 0 && (
              <div style={{ marginTop: 12, display: 'flex', flexWrap: 'wrap', justifyContent: 'center', alignItems: 'center', gap: '4px 8px' }}>
                <span>Or load an example:</span>
                {scheduleExamples.map((ex) => (
                  <Button
                    key={ex.slug}
                    variant="link"
                    isInline
                    isDisabled={!!loadingExampleSlug}
                    isLoading={loadingExampleSlug === ex.slug}
                    onClick={() => handleLoadExample(ex.slug)}
                  >
                    {ex.label}
                  </Button>
                ))}
              </div>
            )}
          </EmptyStateBody>
        </EmptyState>
      )}

      {/* Progress + Cancel/Pause controls */}
      {deploying && (
        <div style={{ marginBottom: 16 }}>
          <Progress value={progress} title={progressMsg} aria-label="Deploy progress" />
          <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
            <Button variant="secondary" size="sm" onClick={handleDeployPause}>
              {deployPaused ? 'Resume' : 'Pause'}
            </Button>
            <Button variant="danger" size="sm" onClick={handleDeployCancel}>
              Cancel
            </Button>
          </div>
        </div>
      )}

      {/* Log */}
      {logLines.length > 0 && (
        <div className="log-box" ref={logRef}>
          {logLines.join('\n')}
        </div>
      )}

      {/* Deploy confirmation modal (live mode only) */}
      <Modal
        variant="medium"
        isOpen={showDeployConfirm}
        onClose={() => setShowDeployConfirm(false)}
        aria-labelledby="deploy-confirm-title"
      >
        <ModalHeader title="Confirm Live Deployment" labelId="deploy-confirm-title" titleIconVariant="warning" />
        <ModalBody>
          <p>
            You are about to run a <strong>live deployment</strong> for {schedules.length} schedule(s).
            This will provision real resources.
          </p>

          {/* BLOCKING ISSUES */}
          {numUsersViolations.length > 0 && (
            <Alert variant="danger" isInline title="Deployment blocked" style={{ margin: '12px 0' }}>
              <p style={{ marginBottom: 8 }}>The following issues must be resolved before deployment:</p>
              <strong>• num_users exceeds catalog maximum</strong>
              <AffectsItemsList
                count={numUsersViolations.length}
                expanded={modalListsExpanded}
                onToggle={() => setModalListsExpanded((v) => !v)}
                noun="schedule"
              >
                {sortByCiName(numUsersViolations).map((v, i) => (
                  <li key={i}>{v.ci_name}: {v.requested_users} users requested, max is {v.maximum}</li>
                ))}
              </AffectsItemsList>
            </Alert>
          )}

          {catalogNotFoundUnique.length > 0 && (
            <Alert variant="warning" isInline title="Unresolved catalog items" style={{ margin: '12px 0' }}>
              <p style={{ marginBottom: 6, fontSize: '0.9rem' }}>
                Prefer <strong>Skip these items</strong> on the upload page first. Deploy auto-applies suggested
                <code> .event</code>/<code>.prod</code> suffixes when known; truly missing CIs still block.
              </p>
              <AffectsItemsList
                count={catalogNotFoundUnique.length}
                expanded={modalListsExpanded}
                onToggle={() => setModalListsExpanded((v) => !v)}
              >
                {sortByCiName(catalogNotFoundUnique).map((nf) => (
                  <li key={nf.ci}>{nf.ci_name} (<code>{nf.ci}</code>)</li>
                ))}
              </AffectsItemsList>
            </Alert>
          )}

          {skippedCatalogSummary && skippedCatalogSummary.items.length > 0 && (
            <Alert variant="info" isInline title={`Will deploy ${skippedCatalogSummary.remaining} row(s) — already skipped ${skippedCatalogSummary.items.length} missing CI(s)`} style={{ margin: '12px 0' }}>
              <AffectsItemsList
                count={skippedCatalogSummary.items.length}
                expanded={modalListsExpanded}
                onToggle={() => setModalListsExpanded((v) => !v)}
              >
                {sortByCiName(skippedCatalogSummary.items).map((item) => (
                  <li key={item.ci}>{item.ci_name} (<code>{item.ci}</code>)</li>
                ))}
              </AffectsItemsList>
            </Alert>
          )}

          {/* WARNINGS (non-blocking) */}
          {(catalogNamespaceMismatches.length > 0 || usersNotInCatalog.filter(a => a.severity === 'high').length > 0 || warnings.length > 0) && (
            <Alert variant="warning" isInline title="Warnings detected" style={{ margin: '12px 0' }}>
              <p style={{ marginBottom: 8 }}>Review these issues before deploying:</p>
              {catalogNamespaceMismatches.length > 0 && (
                <div style={{ marginBottom: 8 }}>
                  <strong>• Catalog namespace mismatches</strong>
                  <AffectsItemsList
                    count={catalogNamespaceMismatches.length}
                    expanded={modalListsExpanded}
                    onToggle={() => setModalListsExpanded((v) => !v)}
                  >
                    {sortByCiName(catalogNamespaceMismatches).map((m, i) => (
                      <li key={i}>{m.ci_name}: expected {m.expected_catalog_namespace}, found in {m.found_catalog_namespace}</li>
                    ))}
                  </AffectsItemsList>
                </div>
              )}
              {usersNotInCatalog.filter(a => a.severity === 'high').length > 0 && (
                <div style={{ marginBottom: 8 }}>
                  <strong>• High-severity Users/Instances issues</strong>
                  <AffectsItemsList
                    count={usersNotInCatalog.filter(a => a.severity === 'high').length}
                    expanded={modalListsExpanded}
                    onToggle={() => setModalListsExpanded((v) => !v)}
                    noun="row"
                  >
                    {sortByCiName(usersNotInCatalog.filter(a => a.severity === 'high')).map((a, i) => (
                      <li key={i}>{a.ci_name}: {a.message}</li>
                    ))}
                  </AffectsItemsList>
                </div>
              )}
              {warnings.length > 0 && (
                <div>
                  <strong>• Date/configuration warnings ({warnings.length}):</strong> Check schedule preview for details
                </div>
              )}
            </Alert>
          )}

          {/* SUCCESS STATE - no issues */}
          {numUsersViolations.length === 0 && catalogNotFound.length === 0 && catalogNamespaceMismatches.length === 0 && usersNotInCatalog.filter(a => a.severity === 'high').length === 0 && warnings.length === 0 && (
            <Alert variant="success" isInline title="Pre-deployment checks passed" style={{ margin: '12px 0' }}>
              No blocking issues or warnings detected. Ready to deploy.
            </Alert>
          )}

          <div style={{ marginTop: 12, fontSize: '0.85rem', maxHeight: 150, overflowY: 'auto' }}>
            <strong>Schedules to deploy:</strong>
            <ul style={{ margin: '4px 0 0', paddingLeft: 20 }}>
              {schedules.slice(0, 10).map((s) => (
                <li key={`${s.ci}-${s.namespace}`}>
                  <strong>{s.ci_name}</strong> — {s.ci} in {s.namespace}
                  {s.instances != null && ` (${s.instances} instances)`}
                  {s.users != null && ` (${s.users} users)`}
                  {s.is_multi_asset && ' [multi-asset]'}
                </li>
              ))}
              {schedules.length > 10 && <li>... and {schedules.length - 10} more</li>}
            </ul>
          </div>

          {hasDeployRisks && !deployBlocked && (
            <div style={{ marginTop: 16 }}>
              <Checkbox
                id="deploy-risk-ack"
                isChecked={deployRiskAck}
                onChange={(_e, checked) => setDeployRiskAck(checked)}
                label="I understand this deploy is not Labagator-clean (skipped / missing CIs or high Users/Instances risks above)"
              />
            </div>
          )}
        </ModalBody>
        <ModalFooter>
          <Button
            variant="danger"
            onClick={handleDeploy}
            isDisabled={deployBlocked || (hasDeployRisks && !deployRiskAck)}
          >
            {deployBlocked
              ? 'Cannot Deploy (blocked)'
              : hasDeployRisks && !deployRiskAck
                ? 'Acknowledge risks to deploy'
                : 'Deploy Now'}
          </Button>
          <Button variant="link" onClick={() => setShowDeployConfirm(false)}>Cancel</Button>
        </ModalFooter>
      </Modal>

      {/* Shared confirm for riskier local bypasses */}
      <Modal
        variant="small"
        isOpen={!!riskConfirm}
        onClose={() => setRiskConfirm(null)}
        aria-labelledby="risk-confirm-title"
      >
        <ModalHeader title={riskConfirm?.title || 'Confirm'} labelId="risk-confirm-title" titleIconVariant="warning" />
        <ModalBody>
          <p style={{ marginBottom: 8 }}>{riskConfirm?.body}</p>
          {riskConfirm && riskConfirm.bullets.length > 0 && (
            <ul style={{ margin: '0 0 0 20px', fontSize: '0.85rem' }}>
              {riskConfirm.bullets.map((b) => (
                <li key={b}>{b}</li>
              ))}
              {/* hint when truncated */}
            </ul>
          )}
          <p style={{ marginTop: 10, fontSize: '0.85rem', color: 'var(--pf-v6-global--Color--200)' }}>
            This is recorded as an operator override for the session audit.
          </p>
        </ModalBody>
        <ModalFooter>
          <Button variant="warning" onClick={() => confirmRiskAction()}>
            Confirm
          </Button>
          <Button variant="link" onClick={() => setRiskConfirm(null)}>Cancel</Button>
        </ModalFooter>
      </Modal>

      {/* Clear confirmation modal */}
      <Modal
        variant="small"
        isOpen={showClearConfirm}
        onClose={() => setShowClearConfirm(false)}
        aria-labelledby="clear-confirm-title"
      >
        <ModalHeader title="Confirm Clear Session" labelId="clear-confirm-title" />
        <ModalBody>
          This will archive the current session and reset all schedules, results, and logs. Continue?
        </ModalBody>
        <ModalFooter>
          <Button variant="primary" onClick={handleClear}>Clear Session</Button>
          <Button variant="link" onClick={() => setShowClearConfirm(false)}>Cancel</Button>
        </ModalFooter>
      </Modal>

      {/* Labagator import confirmation modal */}
      <Modal
        variant="small"
        isOpen={showLabagatorConfirm}
        onClose={() => { setShowLabagatorConfirm(false); setLabagatorPreview(null); }}
        aria-labelledby="labagator-confirm-title"
      >
        <ModalHeader title="Confirm Labagator Import" labelId="labagator-confirm-title" />
        <ModalBody>
          {labagatorPreview && (
            <p>
              Import <strong>{labagatorPreview.session_count}</strong> session(s) from{' '}
              <strong>{labagatorPreview.event_name}</strong> into namespace <strong>{labagatorNamespace}</strong>?
            </p>
          )}
        </ModalBody>
        <ModalFooter>
          <Button variant="primary" onClick={handleLabagatorConfirm} isDisabled={labagatorImporting} isLoading={labagatorImporting}>Confirm</Button>
          <Button variant="link" onClick={() => { setShowLabagatorConfirm(false); setLabagatorPreview(null); }}>Cancel</Button>
        </ModalFooter>
      </Modal>

      {/* Catalog namespace override confirmation modal */}
      <Modal
        variant="medium"
        isOpen={showCatalogOverrideModal}
        onClose={() => {
          setShowCatalogOverrideModal(false);
          setCatalogOverrideAction(null);
        }}
        aria-labelledby="catalog-override-title"
      >
        <ModalHeader
          title="Confirm Catalog Namespace Override"
          labelId="catalog-override-title"
          titleIconVariant={catalogOverrideAction === 'clear' ? undefined : 'warning'}
        />
        <ModalBody>
          {catalogOverrideAction && (() => {
            const newValue = catalogOverrideAction === 'clear' ? 'Auto-detect' :
                           catalogOverrideAction === 'event' ? 'babylon-catalog-event' :
                           catalogOverrideAction === 'prod' ? 'babylon-catalog-prod' :
                           'babylon-catalog-dev';

            // Count workshops by their auto-detected catalog
            const detectionSummary = schedules.reduce((acc, s) => {
              const detected = s.ci.endsWith('.event') ? 'babylon-catalog-event' :
                             s.ci.endsWith('.prod') ? 'babylon-catalog-prod' :
                             s.ci.endsWith('.dev') ? 'babylon-catalog-dev' :
                             'babylon-catalog-prod';
              acc[detected] = (acc[detected] || 0) + 1;
              return acc;
            }, {} as Record<string, number>);

            // Check if override conflicts with auto-detection
            const hasConflict = catalogOverrideAction !== 'clear' && Object.keys(detectionSummary).some(
              ns => ns !== newValue && detectionSummary[ns] > 0
            );

            return (
              <>
                <p>
                  <strong>Action:</strong> {catalogOverrideAction === 'clear' ? 'Remove overrides and use auto-detection' : `Set catalog namespace to ${newValue}`} for <strong>{schedules.length} workshop(s)</strong>
                </p>

                {catalogOverrideAction === 'clear' ? (
                  <>
                    <p>Workshops will use auto-detection based on CI suffix:</p>
                    <ul style={{ marginTop: 8 }}>
                      <li><code>.event</code> suffix → <strong>babylon-catalog-event</strong></li>
                      <li><code>.prod</code> suffix → <strong>babylon-catalog-prod</strong></li>
                      <li><code>.dev</code> suffix → <strong>babylon-catalog-dev</strong></li>
                      <li>No suffix → <strong>babylon-catalog-prod</strong> (default)</li>
                    </ul>
                    <p style={{ marginTop: 12 }}>Current auto-detection:</p>
                    <ul style={{ marginTop: 8 }}>
                      {Object.entries(detectionSummary).map(([ns, count]) => (
                        <li key={ns}>{count} workshop{count > 1 ? 's' : ''} → {ns}</li>
                      ))}
                    </ul>
                  </>
                ) : hasConflict ? (
                  <Alert
                    variant="warning"
                    isInline
                    title="Potential catalog mismatch"
                    style={{ marginTop: 16 }}
                  >
                    <p>Auto-detection suggests these workshops should use:</p>
                    <ul style={{ marginTop: 8 }}>
                      {Object.entries(detectionSummary).map(([ns, count]) => (
                        <li key={ns}>{count} workshop{count > 1 ? 's' : ''} have CI suffix → {ns}</li>
                      ))}
                    </ul>
                    <p style={{ marginTop: 8 }}>
                      You are forcing them to <strong>{newValue}</strong> instead.
                      Catalog items may not be found if they don't exist in {newValue}.
                    </p>
                  </Alert>
                ) : (
                  <Alert
                    variant="success"
                    isInline
                    title="Catalog override matches auto-detection"
                    style={{ marginTop: 16 }}
                  >
                    All {schedules.length} workshop(s) have the appropriate CI suffix for {newValue}.
                  </Alert>
                )}
              </>
            );
          })()}
        </ModalBody>
        <ModalFooter>
          <Button
            variant={catalogOverrideAction === 'clear' ? 'primary' : 'warning'}
            onClick={confirmCatalogOverride}
          >
            {catalogOverrideAction === 'clear' ? 'Yes, Clear Overrides' : 'Yes, Override All'}
          </Button>
          <Button
            variant="link"
            onClick={() => {
              setShowCatalogOverrideModal(false);
              setCatalogOverrideAction(null);
            }}
          >
            Cancel
          </Button>
        </ModalFooter>
      </Modal>

      {/* Fill Missing Dates modal */}
      <Modal
        variant="small"
        isOpen={showFillDatesModal}
        onClose={() => {
          setShowFillDatesModal(false);
          setFillProvDate('');
          setFillStopDate('');
          setFillDestroyDate('');
        }}
        aria-labelledby="fill-dates-title"
      >
        <ModalHeader title="Fill Missing Dates" labelId="fill-dates-title" />
        <ModalBody>
          <p style={{ marginBottom: 16 }}>
            Enter default dates to fill in for all schedules that are missing provisioning, auto-stop, or auto-destroy dates.
            Only empty date fields will be updated.
          </p>
          <Alert variant="info" isInline title="Date format" style={{ marginBottom: 16 }}>
            Use format: <code>DD/MM/YYYY HH:MM</code> (e.g., <code>15/05/2026 14:00</code>)
          </Alert>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            <div>
              <label htmlFor="fill-prov-date" style={{ display: 'block', marginBottom: 4, fontWeight: 600 }}>
                Provisioning Date (UTC)
              </label>
              <TextInput
                id="fill-prov-date"
                value={fillProvDate}
                onChange={(_e, value) => setFillProvDate(value)}
                placeholder="DD/MM/YYYY HH:MM"
              />
            </div>
            <div>
              <label htmlFor="fill-stop-date" style={{ display: 'block', marginBottom: 4, fontWeight: 600 }}>
                Auto-Stop Date (UTC)
              </label>
              <TextInput
                id="fill-stop-date"
                value={fillStopDate}
                onChange={(_e, value) => setFillStopDate(value)}
                placeholder="DD/MM/YYYY HH:MM"
              />
            </div>
            <div>
              <label htmlFor="fill-destroy-date" style={{ display: 'block', marginBottom: 4, fontWeight: 600 }}>
                Auto-Destroy Date (UTC)
              </label>
              <TextInput
                id="fill-destroy-date"
                value={fillDestroyDate}
                onChange={(_e, value) => setFillDestroyDate(value)}
                placeholder="DD/MM/YYYY HH:MM"
              />
            </div>
          </div>
        </ModalBody>
        <ModalFooter>
          <Button variant="primary" onClick={handleFillMissingDates}>
            Apply
          </Button>
          <Button
            variant="link"
            onClick={() => {
              setShowFillDatesModal(false);
              setFillProvDate('');
              setFillStopDate('');
              setFillDestroyDate('');
            }}
          >
            Cancel
          </Button>
        </ModalFooter>
      </Modal>

      {/* TenantClusterPool creation modal */}
      <Modal
        variant="large"
        isOpen={showPoolCreateModal}
        onClose={() => {
          setShowPoolCreateModal(false);
          setPoolCreateYaml('');
          setPoolCreateResults([]);
          setPoolCreateApplied(false);
          setPoolCreateStatusCheck(null);
        }}
        aria-labelledby="pool-create-title"
      >
        <ModalHeader
          title={`Create TenantClusterPools (${poolCreateCIs.length} pool${poolCreateCIs.length === 1 ? '' : 's'})`}
          labelId="pool-create-title"
        />
        <ModalBody>
          <Alert variant="info" isInline title="What this does" style={{ marginBottom: 12 }}>
            Creates a <code>TenantClusterPool</code> CRD in the <code>shared-clusters</code> namespace for each
            missing cluster. Babylon uses these pools to pre-provision and share cluster capacity across events.
            Review the YAML before applying — you can copy it and apply manually, or click Apply to push it directly.
          </Alert>

          {/* Cluster CI list — confirm the right pools before applying */}
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: '0.875rem', fontWeight: 600, marginBottom: 6 }}>
              Pools to create ({poolCreateCIs.length}):
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {poolCreateCIs.map((ci, i) => (
                <span
                  key={i}
                  style={{
                    display: 'inline-block',
                    padding: '2px 10px',
                    borderRadius: 12,
                    fontSize: '0.8rem',
                    fontFamily: 'monospace',
                    background: 'var(--pf-v6-global--BackgroundColor--200)',
                    border: '1px solid var(--pf-v6-global--BorderColor--100)',
                    color: 'var(--pf-v6-global--Color--100)',
                  }}
                >
                  {ci}
                </span>
              ))}
            </div>
          </div>

          {/* Per-pool status check */}
          <div style={{ marginBottom: 16 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
              <span style={{ fontSize: '0.875rem', fontWeight: 600 }}>What will happen</span>
              <Button
                variant="link" isInline isDisabled={poolCreateStatusLoading}
                onClick={async () => {
                  setPoolCreateStatusLoading(true);
                  try {
                    const data = await api.checkPoolStatus(poolCreateCIs);
                    setPoolCreateStatusCheck(data.results || []);
                  } catch { /* ignore */ } finally {
                    setPoolCreateStatusLoading(false);
                  }
                }}
              >
                {poolCreateStatusLoading ? 'Checking…' : 'Check cluster'}
              </Button>
            </div>
            {poolCreateStatusCheck ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                {poolCreateStatusCheck.map((s, i) => {
                  const badge = s.action_preview === 'error'
                    ? { label: 'Lookup failed. Pool state is unknown.', color: 'var(--pf-v6-global--danger-color--100)' }
                    : s.action_preview === 'already_exists'
                    ? { label: 'Reference exists; no changes', color: 'var(--pf-v6-global--success-color--100)' }
                    : s.action_preview === 'enable'
                    ? { label: `Exists – disabled → will enable${s.available_clusters > 0 ? ` (${s.available_clusters} clusters available)` : ''}`, color: 'var(--pf-v6-global--warning-color--100)' }
                    : { label: 'Does not exist → will create fresh', color: 'var(--pf-v6-global--info-color--100)' };
                  return (
                    <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.8rem' }}>
                      <code style={{ flex: '0 0 auto', maxWidth: 340, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.name}</code>
                      <span style={{ color: badge.color, fontWeight: 600 }}>{badge.label}</span>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div style={{ fontSize: '0.8rem', color: 'var(--pf-v6-global--Color--200)' }}>
                Click "Check cluster" to see whether each pool already exists before applying.
              </div>
            )}
          </div>

          {/* Config form */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px 24px', marginBottom: 16 }}>
            <div>
              <label style={{ display: 'block', fontWeight: 600, marginBottom: 4, fontSize: '0.875rem' }}>Cloud</label>
              <select
                value={poolCreateCloud}
                onChange={e => setPoolCreateCloud(e.target.value)}
                style={{ width: '100%', padding: '6px 8px', borderRadius: 4, border: '1px solid var(--pf-v6-global--BorderColor--100)', background: 'var(--pf-v6-global--BackgroundColor--100)', color: 'var(--pf-v6-global--Color--100)' }}
              >
                <option value="cnv-dedicated-shared">cnv-dedicated-shared (default)</option>
                <option value="aws">aws</option>
                <option value="osp">osp (OpenStack)</option>
                <option value="azure">azure</option>
                <option value="gcp">gcp</option>
              </select>
            </div>
            <div>
              <label style={{ display: 'block', fontWeight: 600, marginBottom: 4, fontSize: '0.875rem' }}>Environment Level</label>
              <select
                value={poolCreateEnvLevel}
                onChange={e => setPoolCreateEnvLevel(e.target.value)}
                style={{ width: '100%', padding: '6px 8px', borderRadius: 4, border: '1px solid var(--pf-v6-global--BorderColor--100)', background: 'var(--pf-v6-global--BackgroundColor--100)', color: 'var(--pf-v6-global--Color--100)' }}
              >
                <option value="integration">integration</option>
                <option value="production">production</option>
              </select>
            </div>
            <div>
              <label style={{ display: 'block', fontWeight: 600, marginBottom: 4, fontSize: '0.875rem' }}>Min Clusters</label>
              <input type="number" min={0} max={10} value={poolCreateMin}
                onChange={e => setPoolCreateMin(Number(e.target.value))}
                style={{ width: '100%', padding: '6px 8px', borderRadius: 4, border: '1px solid var(--pf-v6-global--BorderColor--100)', background: 'var(--pf-v6-global--BackgroundColor--100)', color: 'var(--pf-v6-global--Color--100)' }} />
            </div>
            <div>
              <label style={{ display: 'block', fontWeight: 600, marginBottom: 4, fontSize: '0.875rem' }}>Max Clusters</label>
              <input type="number" min={1} max={20} value={poolCreateMax}
                onChange={e => setPoolCreateMax(Number(e.target.value))}
                style={{ width: '100%', padding: '6px 8px', borderRadius: 4, border: '1px solid var(--pf-v6-global--BorderColor--100)', background: 'var(--pf-v6-global--BackgroundColor--100)', color: 'var(--pf-v6-global--Color--100)' }} />
            </div>
            <div>
              <label style={{ display: 'block', fontWeight: 600, marginBottom: 4, fontSize: '0.875rem' }}>Min Available Placements</label>
              <input type="number" min={0} max={20} value={poolCreateMinAvailPlacements}
                onChange={e => setPoolCreateMinAvailPlacements(Number(e.target.value))}
                style={{ width: '100%', padding: '6px 8px', borderRadius: 4, border: '1px solid var(--pf-v6-global--BorderColor--100)', background: 'var(--pf-v6-global--BackgroundColor--100)', color: 'var(--pf-v6-global--Color--100)' }} />
              <div style={{ fontSize: '0.75rem', color: 'var(--pf-v6-global--Color--200)', marginTop: 3 }}>Operator keeps this many tenant slots ready at all times</div>
            </div>
            <div>
              <label style={{ display: 'block', fontWeight: 600, marginBottom: 4, fontSize: '0.875rem' }}>Max Placements (tenants per cluster)</label>
              <input type="number" min={1} max={50} value={poolCreateMaxPlacements}
                onChange={e => setPoolCreateMaxPlacements(Number(e.target.value))}
                style={{ width: '100%', padding: '6px 8px', borderRadius: 4, border: '1px solid var(--pf-v6-global--BorderColor--100)', background: 'var(--pf-v6-global--BackgroundColor--100)', color: 'var(--pf-v6-global--Color--100)' }} />
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, paddingTop: 24 }}>
              <Switch
                id="pool-create-enabled"
                isChecked={poolCreateEnabled}
                onChange={(_e, checked) => setPoolCreateEnabled(checked)}
                label="Enable pool immediately"
              />
            </div>
          </div>

          <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
            <Button
              variant="secondary"
              isLoading={poolCreateLoading && !poolCreateApplied}
              isDisabled={poolCreateLoading}
              onClick={async () => {
                setPoolCreateLoading(true);
                setPoolCreateApplied(false);
                try {
                  const res = await api.createTenantClusterPools({
                    cluster_cis: poolCreateCIs,
                    enabled: poolCreateEnabled,
                    min_clusters: poolCreateMin,
                    max_clusters: poolCreateMax,
                    min_available_sandbox_placements: poolCreateMinAvailPlacements,
                    max_placements: poolCreateMaxPlacements,
                    environment_level: poolCreateEnvLevel,
                    cloud: poolCreateCloud,
                    apply_to_cluster: false,
                  });
                  setPoolCreateYaml(res.yaml);
                  setPoolCreateResults([]);
                } catch (e) {
                  showToast(`Failed to generate YAML: ${e}`, 'danger');
                } finally {
                  setPoolCreateLoading(false);
                }
              }}
            >
              Preview YAML
            </Button>
            <Button
              variant="primary"
              isLoading={poolCreateLoading && poolCreateApplied}
              isDisabled={poolCreateLoading}
              onClick={async () => {
                setPoolCreateLoading(true);
                setPoolCreateApplied(true);
                try {
                  const res = await api.createTenantClusterPools({
                    cluster_cis: poolCreateCIs,
                    enabled: poolCreateEnabled,
                    min_clusters: poolCreateMin,
                    max_clusters: poolCreateMax,
                    min_available_sandbox_placements: poolCreateMinAvailPlacements,
                    max_placements: poolCreateMaxPlacements,
                    environment_level: poolCreateEnvLevel,
                    cloud: poolCreateCloud,
                    apply_to_cluster: true,
                  });
                  setPoolCreateYaml(res.yaml);
                  setPoolCreateResults(res.results);
                  const allOk = res.results.every(r => r.success);
                  if (allOk) {
                    const created = res.results.filter(r => r.action === 'created').length;
                    const enabled = res.results.filter(r => r.action === 'enabled').length;
                    const active = res.results.filter(r => r.action === 'already_active').length;
                    const parts = [];
                    if (created) parts.push(`${created} created`);
                    if (enabled) parts.push(`${enabled} existing pool(s) enabled`);
                    if (active) parts.push(`${active} already active`);
                    showToast(`Done — ${parts.join(', ')}. Babylon will provision clusters (30–60 min). Use “−4h earlier” on the warning if you want runway before event times (extra cost).`, 'success');
                    // Do NOT re-validate here: pool CRD exists but has no ready clusters yet.
                    // Do NOT auto-shift times — optional “−4h earlier” on the warning only.
                  } else {
                    showToast('Some pools failed to apply — see results below', 'danger');
                  }
                } catch (e) {
                  showToast(`Apply failed: ${e}`, 'danger');
                } finally {
                  setPoolCreateLoading(false);
                }
              }}
            >
              Apply to Cluster
            </Button>
          </div>

          {poolCreateResults.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              {poolCreateResults.map((r, i) => {
                const actionLabel = r.action === 'enabled'
                  ? 'Existing pool enabled'
                  : r.action === 'already_active'
                  ? 'Already active'
                  : 'Pool created';
                const variant = r.success ? (r.action === 'already_active' ? 'info' : 'success') : 'danger';
                const title = r.success ? `${actionLabel}: ${r.name}` : `Failed: ${r.name}`;
                return (
                  <Alert key={i} variant={variant} isInline title={title} style={{ marginBottom: 4 }}>
                    {r.success ? r.output : r.error}
                  </Alert>
                );
              })}

              {poolCreateResults.some(r => r.success && r.action === 'already_active') && (
                <Alert variant="info" isInline title="Some pools were already active" style={{ marginTop: 8 }}>
                  These pools already exist and are enabled — no changes were made. If clusters are not yet available,
                  the Babylon operator may still be provisioning them (this takes 30–60 min). Check the pool status
                  in the cluster console under <code>shared-clusters</code>.
                </Alert>
              )}

              {poolCreateResults.some(r => r.success && r.action === 'enabled') && (
                <Alert variant="warning" isInline title="Existing pools enabled — clusters not ready yet" style={{ marginTop: 8 }}>
                  <p style={{ margin: '0 0 6px' }}>
                    These pools already existed but were disabled. Flow has patched them to <code>enabled: true</code> — their
                    other settings (max clusters, placements, cloud) were left unchanged to avoid overwriting manual config.
                  </p>
                  <ol style={{ margin: '4px 0 0 18px', fontSize: '0.875rem', lineHeight: 1.6 }}>
                    <li>Babylon will now start provisioning clusters. This takes <strong>30–60 minutes</strong>.</li>
                    <li>Once clusters are ready, close this modal and click <strong>Re-check cluster refs</strong>.</li>
                    <li>Then deploy your workshops normally.</li>
                  </ol>
                </Alert>
              )}

              {poolCreateResults.some(r => r.success && r.action === 'created') && (
                <Alert variant="warning" isInline title="New pools created — clusters not ready yet" style={{ marginTop: 8 }}>
                  <p style={{ margin: '0 0 6px' }}>
                    {poolCreateEnabled
                      ? 'Pools are created and enabled. Babylon will begin provisioning clusters immediately.'
                      : <>Pools are created but <strong>disabled</strong>. Go to the cluster console and set <code>spec.enabled: true</code> on each pool in the <code>shared-clusters</code> namespace to start provisioning.</>
                    }
                  </p>
                  <ol style={{ margin: '4px 0 0 18px', fontSize: '0.875rem', lineHeight: 1.6 }}>
                    <li>Cluster provisioning typically takes <strong>30–60 minutes</strong>.</li>
                    <li>Once clusters are ready, close this modal and click <strong>Re-check cluster refs</strong>.</li>
                    <li>Then deploy your workshops normally.</li>
                  </ol>
                </Alert>
              )}

              {poolCreateTimeShifted > 0 && (
                <Alert variant="warning" isInline title={`Schedule drift: −4h earlier on ${poolCreateTimeShifted} workshop${poolCreateTimeShifted === 1 ? '' : 's'}`} style={{ marginTop: 8 }}>
                  Provisioning and stop/destroy were moved earlier than Labagator/event times (extra cluster hours before students — cost).
                  Not applied unless you click <strong>−4h earlier</strong> on the warning.
                </Alert>
              )}

              {poolCreateResults.some(r => !r.success) && (
                <Alert variant="danger" isInline title="One or more pools failed" style={{ marginTop: 8 }}>
                  Check that you are logged in to the correct cluster and have access to the <code>shared-clusters</code> namespace.
                  The YAML is shown below — you can copy it and apply manually with <code>oc apply -f -</code>.
                </Alert>
              )}
            </div>
          )}

          {poolCreateYaml && (
            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                <span style={{ fontSize: '0.875rem', fontWeight: 600 }}>Generated YAML</span>
                <Button variant="link" isInline onClick={() => navigator.clipboard.writeText(poolCreateYaml)}>
                  Copy to clipboard
                </Button>
              </div>
              <pre style={{
                background: 'var(--pf-v6-global--BackgroundColor--200)',
                border: '1px solid var(--pf-v6-global--BorderColor--100)',
                borderRadius: 4,
                padding: '12px 14px',
                fontSize: '0.78rem',
                overflow: 'auto',
                maxHeight: 320,
                whiteSpace: 'pre',
                fontFamily: 'monospace',
              }}>
                {poolCreateYaml}
              </pre>
            </div>
          )}
        </ModalBody>
        <ModalFooter>
          <Button
            variant="link"
            onClick={() => {
              setShowPoolCreateModal(false);
              setPoolCreateYaml('');
              setPoolCreateResults([]);
              setPoolCreateApplied(false);
            }}
          >
            Close
          </Button>
        </ModalFooter>
      </Modal>

    </PageSection>
  );
};
