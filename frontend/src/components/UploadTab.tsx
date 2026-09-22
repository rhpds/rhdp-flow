import { useState, useRef, useEffect, useCallback, useMemo, Fragment } from 'react';
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardTitle,
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
import type { WorkshopSchedule, DeploymentResult, NumUsersViolation, UsersNotInCatalogAdvisory, ScheduleExampleMeta, LabagatorEventSummary, LabagatorPreviewResponse } from '../types';

/* ── Schedule date validation helpers ── */

interface ScheduleWarning {
  index: number;
  field: string;
  message: string;
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

interface Props {
  dryRun: boolean;
  schedules: WorkshopSchedule[];
  setSchedules: (s: WorkshopSchedule[]) => void;
  results: DeploymentResult[];
  setResults: (r: DeploymentResult[]) => void;
  showToast: (msg: string, variant: 'success' | 'danger' | 'info') => void;
  onClear: () => void;
  setDeployLogFile?: (f: string | null) => void;
}

export const UploadTab: React.FC<Props> = ({
  dryRun, schedules, setSchedules, setResults, showToast, onClear, setDeployLogFile,
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

  useEffect(() => {
    api.listLabagatorEvents()
      .then((res) => {
        setLabagatorUnavailable(!!res.error);
        setLabagatorEvents(res.events);
      })
      .catch(() => setLabagatorUnavailable(true));
  }, []);

  const [deploying, setDeploying] = useState(false);
  const [deployPaused, setDeployPaused] = useState(false);
  const [validating, setValidating] = useState(false);
  const [yamlDownloading, setYamlDownloading] = useState(false);
  const [rowEditsLocked, setRowEditsLocked] = useState(false);
  const [scheduleExamples, setScheduleExamples] = useState<ScheduleExampleMeta[]>([]);
  const [loadingExampleSlug, setLoadingExampleSlug] = useState<string | null>(null);
  const [passwordCount, setPasswordCount] = useState<number | null>(null);

  // Labagator import state (live API import)
  const [labagatorEvents, setLabagatorEvents] = useState<LabagatorEventSummary[]>([]);
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
  const [ignoreCapacityWarnings, setIgnoreCapacityWarnings] = useState(false);

  // Multi-cluster deploy target picker (Feature 2 — identity-gated to approved operators)
  const [pickerAllowed, setPickerAllowed] = useState(false);
  const [deployClusters, setDeployClusters] = useState<import('../types').ClusterTarget[]>([]);
  const [targetCluster, setTargetCluster] = useState<string>(getSelectedTarget);

  useEffect(() => {
    let cancelled = false;
    api.getClusters()
      .then((resp) => {
        if (cancelled) return;
        setPickerAllowed(resp.allowed);
        setDeployClusters(resp.clusters ?? []);
      })
      .catch(() => {
        if (cancelled) return;
        setPickerAllowed(false);
        setDeployClusters([]);
      });
    return () => { cancelled = true; };
  }, []);

  // Namespace validation
  const [missingNamespaces, setMissingNamespaces] = useState<string[]>([]);

  // num_users limit validation
  const [numUsersViolations, setNumUsersViolations] = useState<NumUsersViolation[]>([]);
  const [usersNotInCatalog, setUsersNotInCatalog] = useState<UsersNotInCatalogAdvisory[]>([]);
  const [numUsersLimits, setNumUsersLimits] = useState<Record<string, number>>({});

  // Catalog namespace validation
  const [catalogNamespaceMismatches, setCatalogNamespaceMismatches] = useState<import('../types').CatalogNamespaceMismatch[]>([]);
  const [catalogNotFound, setCatalogNotFound] = useState<Array<{ ci_name: string; ci: string; namespace: string; expected_catalog_namespace: string; message: string }>>([]);

  const [clusterNeeds, setClusterNeeds] = useState<any>(null);
  const [missingTenantRefs, setMissingTenantRefs] = useState<any>(null);

  // Pool capacity validation
  const [poolCapacityWarnings, setPoolCapacityWarnings] = useState<import('../types').PoolCapacityWarning[]>([]);
  const [poolsNotFound, setPoolsNotFound] = useState<import('../types').PoolNotFoundWarning[]>([]);

  // Confirmation modal state
  const [showDeployConfirm, setShowDeployConfirm] = useState(false);
  const [showClearConfirm, setShowClearConfirm] = useState(false);

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

  // ── Schedule validation warnings ──
  const warnings = useMemo(() => {
    const warns: ScheduleWarning[] = [];
    const now = new Date();

    // Detect duplicate rows (same CI + Namespace)
    const seen = new Map<string, number>();
    schedules.forEach((s, i) => {
      const key = `${s.ci}||${s.namespace}`;
      if (seen.has(key) && !s.multi_workshop_name) {
        warns.push({ index: i, field: 'ci', message: `"${s.ci_name}" appears to be a duplicate (same CI + Namespace as row ${(seen.get(key) ?? 0) + 1})` });
      } else {
        seen.set(key, i);
      }
    });

    schedules.forEach((s, i) => {
      const prov = parseScheduleDate(s.provisioning_date);
      const stop = parseScheduleDate(s.auto_stop);
      const destroy = parseScheduleDate(s.auto_destroy);

      // Unparseable dates
      if (s.provisioning_date && !prov)
        warns.push({ index: i, field: 'provisioning_date', message: `"${s.ci_name}" has an unparseable provisioning date: "${s.provisioning_date}"` });
      if (s.auto_stop && !stop)
        warns.push({ index: i, field: 'auto_stop', message: `"${s.ci_name}" has an unparseable auto-stop date: "${s.auto_stop}"` });
      if (s.auto_destroy && !destroy)
        warns.push({ index: i, field: 'auto_destroy', message: `"${s.ci_name}" has an unparseable auto-destroy date: "${s.auto_destroy}"` });

      // Past provisioning date (dates in CSV are UTC; now is also UTC internally)
      if (prov && prov < now)
        warns.push({ index: i, field: 'provisioning_date', message: `"${s.ci_name}" provisioning date is in the past — ${s.provisioning_date} UTC has already passed` });

      // Auto-stop before provisioning
      if (prov && stop && stop <= prov)
        warns.push({ index: i, field: 'auto_stop', message: `"${s.ci_name}" auto-stop is before or equal to provisioning date` });

      // Missing required dates
      if (!s.provisioning_date?.trim())
        warns.push({ index: i, field: 'provisioning_date', message: `"${s.ci_name}" is missing a provisioning date` });
      if (!s.auto_stop?.trim())
        warns.push({ index: i, field: 'auto_stop', message: `"${s.ci_name}" is missing an auto-stop date` });
      if (!s.auto_destroy?.trim())
        warns.push({ index: i, field: 'auto_destroy', message: `"${s.ci_name}" is missing an auto-destroy date` });

      // CI format check (expect vendor.item.env pattern)
      if (s.ci && !s.ci.includes('.'))
        warns.push({ index: i, field: 'ci', message: `"${s.ci_name}" CI "${s.ci}" may be invalid (expected format: vendor.item.env)` });

      // Namespace format check
      if (s.namespace && !/^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/.test(s.namespace))
        warns.push({ index: i, field: 'namespace', message: `"${s.ci_name}" namespace "${s.namespace}" may be invalid (must be lowercase alphanumeric with hyphens)` });

      // Users reasonableness
      if (s.users !== null && s.users > 500)
        warns.push({ index: i, field: 'users', message: `"${s.ci_name}" has a high user count (${s.users}) — verify this is intentional` });
      if (s.users !== null && s.users < 1)
        warns.push({ index: i, field: 'users', message: `"${s.ci_name}" has an invalid user count (${s.users})` });

      // num_users catalog limit check
      if (s.users !== null && s.ci in numUsersLimits && s.users > numUsersLimits[s.ci])
        warns.push({ index: i, field: 'users', message: `"${s.ci_name}" exceeds catalog limit: ${s.users} users requested, max ${numUsersLimits[s.ci]}` });

      // Blank optional fields - removed informational warnings as these fields have defaults
    });
    return warns;
  }, [schedules, numUsersLimits]);

  const warningRowIndices = useMemo(() => new Set(warnings.map(w => w.index)), [warnings]);

  // Detect if there are missing date warnings
  const hasMissingDateWarnings = useMemo(() => {
    return warnings.some(w =>
      w.field === 'provisioning_date' && w.message.includes('missing') ||
      w.field === 'auto_stop' && w.message.includes('missing') ||
      w.field === 'auto_destroy' && w.message.includes('missing')
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
    setNumUsersLimits({});
    setCatalogNamespaceMismatches([]);
    setCatalogNotFound([]);
    setPoolCapacityWarnings([]);
    setPoolsNotFound([]);
    const [nsRes, nuRes, cnRes, pcRes] = await Promise.all([
      api.validateNamespaces(),
      api.validateNumUsers(),
      api.validateCatalogNamespaces(),
      api.validatePoolCapacity(),
    ]);
    if (nsRes.missing.length) setMissingNamespaces(nsRes.missing);
    if (nuRes.violations.length) setNumUsersViolations(nuRes.violations);
    if (nuRes.users_not_in_catalog?.length) setUsersNotInCatalog(nuRes.users_not_in_catalog);
    if (Object.keys(nuRes.limits).length) setNumUsersLimits(nuRes.limits);
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
    if (cnRes.not_found.length) setCatalogNotFound(cnRes.not_found);
    if (pcRes.warnings?.length) setPoolCapacityWarnings(pcRes.warnings);
    if (pcRes.not_found?.length) setPoolsNotFound(pcRes.not_found);
    return { nsRes, nuRes, cnRes, pcRes };
  }, []);

  const handleValidate = async () => {
    if (schedules.length === 0) {
      showToast('Upload a CSV first', 'danger');
      return;
    }
    setValidating(true);
    try {
      const { nsRes, nuRes, cnRes, pcRes } = await refreshClusterValidation();
      const refs = await api.checkTenantClusterRefs(targetCluster);
      setMissingTenantRefs(refs);
      if (cnRes.mismatches.length || cnRes.not_found.length || pcRes.not_found.length || pcRes.warnings.length) {
        showToast('Prerequisite checks found issues on the selected target. Review the alerts below.', 'danger');
        return;
      }
      const nNs = nsRes.missing.length;
      const nNu = nuRes.violations.length;
      const nAdv = nuRes.users_not_in_catalog?.length ?? 0;
      if (nNs === 0 && nNu === 0 && nAdv === 0) {
        showToast(
          'Validation passed: namespaces found on cluster; num_users within catalog limits where checked.',
          'success',
        );
      } else {
        showToast(
          `Validation: ${nNs} missing namespace(s), ${nNu} num_users over limit, ${nAdv} catalog/Users mismatch — see alerts below.`,
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
      });
      if (preview.session_count === 0) {
        showToast('No sessions found for this event this week.', 'info');
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
      const job = await api.deploy({ dry_run: dryRun, resource_lock: resourceLock, enable_resource_pools: enableResourcePools, white_glove: whiteGlove, redirect, showroom_novnc: showroomNovnc, showroom_zerotouch: showroomZerotouch, ignore_capacity_warnings: ignoreCapacityWarnings, target_cluster: targetCluster || null });
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
          ) : labagatorEvents.length === 0 ? (
            <Alert variant="info" isInline title="No Labagator events this week" />
          ) : (
            <>
              <Split hasGutter style={{ marginBottom: 12, alignItems: 'flex-end' }}>
                <SplitItem isFilled>
                  <FormSelect
                    aria-label="Labagator event"
                    value={selectedEventId ?? ''}
                    onChange={(_e, v) => setSelectedEventId(v ? Number(v) : null)}
                  >
                    <FormSelectOption key="" value="" label="Select an event…" />
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
                    variant="primary"
                    isDisabled={!labagatorNamespace || labagatorPreviewing}
                    isLoading={labagatorPreviewing}
                    onClick={handleLabagatorPreview}
                  >
                    Import
                  </Button>
                </SplitItem>
              </Split>
              {labagatorNamespaceError && (
                <Alert variant="danger" isInline title={labagatorNamespaceError} style={{ marginBottom: 12 }} />
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

          {/* Validation warnings */}
          {warnings.length > 0 && (
            <Alert variant="warning" isInline title={`${warnings.length} validation warning(s) — review before deploying`} style={{ marginBottom: 12 }}>
              <ul style={{ margin: '4px 0 0', paddingLeft: 20, fontSize: '0.85rem' }}>
                {warnings.map((w, i) => <li key={i}>{w.message}</li>)}
              </ul>
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
            <Alert variant="danger" isInline title={`${numUsersViolations.length} schedule(s) exceed num_users limit`} style={{ marginBottom: 12 }}>
              <ul style={{ margin: '4px 0 0', paddingLeft: 20, fontSize: '0.85rem' }}>
                {numUsersViolations.map((v, i) => (
                  <li key={i}>
                    <strong>{v.ci_name}</strong> ({v.ci}): {v.requested_users} users requested, catalog max is {v.maximum}
                  </li>
                ))}
              </ul>
              Deployment will be blocked until user counts are reduced below the catalog limit.
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
                  <div style={{ marginBottom: 8, fontWeight: 600 }}>Capacity warnings:</div>
                  <ul style={{ margin: '4px 0 0', paddingLeft: 20, fontSize: '0.85rem' }}>
                    {poolCapacityWarnings.map((w, i) => (
                      <li key={i} style={{ color: w.severity === 'critical' ? 'var(--pf-v6-global--danger-color--100)' : undefined }}>
                        <strong>{w.ci_name}</strong> ({w.pool_name}):
                        Pool {w.pool_saturation_percent}% saturated, {w.placement_capacity_percent}% utilized
                        {w.severity === 'critical' && ' — CRITICAL: at capacity!'}
                      </li>
                    ))}
                  </ul>
                </>
              )}
              {poolsNotFound.length > 0 && (
                <>
                  <div style={{ marginTop: poolCapacityWarnings.length > 0 ? 12 : 0, marginBottom: 8, fontWeight: 600 }}>
                    Pools not found:
                  </div>
                  <ul style={{ margin: '4px 0 0', paddingLeft: 20, fontSize: '0.85rem' }}>
                    {poolsNotFound.map((p, i) => (
                      <li key={i}>
                        <strong>{p.ci_name}</strong>
                      </li>
                    ))}
                  </ul>
                  <div style={{ marginTop: 8, fontSize: '0.85rem', fontStyle: 'italic', color: 'var(--pf-v6-global--Color--200)' }}>
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
            >
              <ul style={{ margin: '4px 0 0', paddingLeft: 20, fontSize: '0.85rem' }}>
                {usersNotInCatalog.map((a, i) => (
                  <li key={i}>{a.message}</li>
                ))}
              </ul>
            </Alert>
          )}

          {/* Catalog namespace mismatches — auto-corrected at deploy time */}
          {catalogNamespaceMismatches.length > 0 && (
            <Alert
              variant="info"
              isInline
              title={`Catalog namespace auto-corrected for ${catalogNamespaceMismatches.length} item(s)`}
              style={{ marginBottom: 12 }}
            >
              <ul style={{ margin: '0 0 10px 20px', fontSize: '0.9rem' }}>
                {catalogNamespaceMismatches.slice(0, 5).map((m, i) => (
                  <li key={i}>
                    <strong>{m.ci_name}</strong>: not in <code>{m.expected_catalog_namespace}</code>, found in <code>{m.found_catalog_namespace}</code> — deploy will use {m.found_catalog_namespace}
                  </li>
                ))}
                {catalogNamespaceMismatches.length > 5 && (
                  <li style={{ fontStyle: 'italic' }}>...and {catalogNamespaceMismatches.length - 5} more</li>
                )}
              </ul>
              <div style={{ fontSize: '0.85rem', color: 'var(--pf-v6-global--Color--200)' }}>
                The catalog namespace column has been updated in the table above. Flow will deploy from the corrected namespace — no action needed.
              </div>
            </Alert>
          )}

          {/* Catalog items not found — blocks deploy */}
          {catalogNotFound.length > 0 && (
            <Alert
              variant="danger"
              isInline
              title={`Deploy blocked — ${catalogNotFound.length} catalog item(s) not found in any namespace`}
              style={{ marginBottom: 12 }}
            >
              <ul style={{ margin: '4px 0 8px', paddingLeft: 20, fontSize: '0.85rem' }}>
                {catalogNotFound.map((nf, i) => (
                  <li key={i}>
                    <strong>{nf.ci_name}</strong> (<code>{nf.ci}</code>)<br />
                    <span style={{ color: 'var(--pf-v6-global--danger-color--100)' }}>{nf.message}</span>
                  </li>
                ))}
              </ul>
              <strong>These catalog items do not exist on this cluster. Remove them from your CSV or wait until they are published before deploying.</strong>
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

          {/* Tenant cluster readiness — a tenant needs somewhere to run:
              either a shared cluster pool exists, OR its cluster provisioner
              deploys in this same batch. If neither, it will fail. */}
          {missingTenantRefs && (() => {
            const all = [
              ...(missingTenantRefs.missing_refs || []),
              ...(missingTenantRefs.ref_no_pool || []),
            ];
            const willFail = all.filter((r: any) => !r.pool_exists && !r.has_cluster_row);
            const viaFreshCluster = all.filter((r: any) => !r.pool_exists && r.has_cluster_row);

            if (willFail.length === 0 && viaFreshCluster.length === 0) return null;

            return (
              <>
                {willFail.length > 0 && (
                  <Alert
                    variant="danger"
                    isInline
                    title={`${willFail.length} workshop(s) will fail — no cluster to run on`}
                    style={{ marginBottom: 12 }}
                    actionLinks={
                      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                        <Button
                          variant="danger"
                          size="sm"
                          onClick={() => {
                            // Use cluster_ref from CatalogItem if available; otherwise
                            // derive from tenant CI by swapping -tenant. → -cluster.
                            // Never fall back to the tenant CI itself — that would create
                            // a pool named after the tenant, which Babylon can't provision.
                            const cis = [...new Set(willFail.map((r: any) => {
                              if (r.cluster_ref) return r.cluster_ref;
                              const derived = (r.ci as string).replace(/-tenant\./, '-cluster.');
                              return derived !== r.ci ? derived : null;
                            }).filter(Boolean))] as string[];
                            setPoolCreateCIs(cis);
                            setPoolCreateYaml('');
                            setPoolCreateResults([]);
                            setPoolCreateApplied(false);
                            setShowPoolCreateModal(true);
                          }}
                        >
                          {(() => {
                            const cis = [...new Set(willFail.map((r: any) => {
                              if (r.cluster_ref) return r.cluster_ref;
                              const derived = (r.ci as string).replace(/-tenant\./, '-cluster.');
                              return derived !== r.ci ? derived : null;
                            }).filter(Boolean))];
                            return `Create ${cis.length} TenantClusterPool${cis.length === 1 ? '' : 's'}`;
                          })()}
                        </Button>
                        <Button
                          variant="link"
                          size="sm"
                          onClick={async () => {
                            try { setMissingTenantRefs(await api.checkTenantClusterRefs(targetCluster)); } catch { /* ignore */ }
                          }}
                        >
                          Re-check cluster refs
                        </Button>
                      </div>
                    }
                  >
                    <div style={{ marginBottom: 8 }}>
                      These tenant workshops need a cluster to run on, but no <code>TenantClusterPool</code> exists
                      in <code>shared-clusters</code> and no matching cluster CI is in this CSV:
                    </div>
                    <ul style={{ margin: '0 0 10px 20px', fontSize: '0.9rem' }}>
                      {willFail.slice(0, 5).map((ref: any, i: number) => (
                        <li key={i}><strong>{ref.workshop_name}</strong></li>
                      ))}
                      {willFail.length > 5 && (
                        <li style={{ color: 'var(--pf-v6-global--Color--200)' }}>
                          ...and {willFail.length - 5} more
                        </li>
                      )}
                    </ul>
                    <div style={{ fontSize: '0.8rem', color: 'var(--pf-v6-global--Color--200)' }}>
                      Fix: click <strong>Create TenantClusterPools</strong> above to create the shared pool (Babylon will provision clusters automatically),
                      or add the matching <code>-cluster.*</code> CI row to your CSV to deploy a dedicated cluster alongside.
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
                      Babylon will route the tenant onto that dedicated cluster:
                    </div>
                    <ul style={{ margin: '0 0 6px 20px', fontSize: '0.9rem' }}>
                      {viaFreshCluster.slice(0, 5).map((ref: any, i: number) => (
                        <li key={i}><strong>{ref.workshop_name}</strong></li>
                      ))}
                      {viaFreshCluster.length > 5 && (
                        <li style={{ color: 'var(--pf-v6-global--Color--200)' }}>
                          ...and {viaFreshCluster.length - 5} more
                        </li>
                      )}
                    </ul>
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
                  <Tooltip content="Skip tenant cluster capacity checks before deployment. Use when deploying to existing clusters with known availability.">
                    <Switch
                      id="ignore-capacity-warnings-switch"
                      label="Ignore Cluster Capacity Warnings"
                      isChecked={ignoreCapacityWarnings}
                      onChange={(_e, checked) => setIgnoreCapacityWarnings(checked)}
                    />
                  </Tooltip>
                </SplitItem>
                {pickerAllowed && deployClusters.length > 0 && (
                  <SplitItem>
                    <Tooltip content="Choose which physical cluster to deploy to. Restricted to approved operators. Defaults to this app's own cluster.">
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
                            setNumUsersLimits({});
                            setCatalogNamespaceMismatches([]);
                            setCatalogNotFound([]);
                            setPoolCapacityWarnings([]);
                            setPoolsNotFound([]);
                            setMissingTenantRefs(null);
                            setClusterNeeds(null);
                            setPoolLookupData({});
                            setAllPools([]);
                            setShowPoolCreateModal(false);
                            showToast('Target changed. Check prerequisites before deploying.', 'info');
                          }}
                          style={{ width: 'auto', minWidth: 180 }}
                        >
                          <FormSelectOption value="" label="This cluster (default)" />
                          {deployClusters.map((c) => (
                            <FormSelectOption key={c.key} value={c.key} label={c.display_name} />
                          ))}
                        </FormSelect>
                      </span>
                    </Tooltip>
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
              <Button variant="primary" onClick={handleDeploy} isDisabled={deploying || validating || yamlDownloading} isDanger={!dryRun}>
                {dryRun ? 'Run dry-run' : 'Deploy'}
              </Button>
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
          {(numUsersViolations.length > 0 || catalogNotFound.length > 0) && (
            <Alert variant="danger" isInline title="Deployment blocked" style={{ margin: '12px 0' }}>
              <p style={{ marginBottom: 8 }}>The following issues must be resolved before deployment:</p>
              {numUsersViolations.length > 0 && (
                <div style={{ marginBottom: 8 }}>
                  <strong>• num_users exceeds catalog maximum ({numUsersViolations.length}):</strong>
                  <ul style={{ margin: '4px 0 0', paddingLeft: 20, fontSize: '0.85rem' }}>
                    {numUsersViolations.slice(0, 3).map((v, i) => (
                      <li key={i}>{v.ci_name}: {v.requested_users} users requested, max is {v.maximum}</li>
                    ))}
                    {numUsersViolations.length > 3 && <li>... and {numUsersViolations.length - 3} more</li>}
                  </ul>
                </div>
              )}
              {catalogNotFound.length > 0 && (
                <div>
                  <strong>• Catalog items not found ({catalogNotFound.length}):</strong>
                  <ul style={{ margin: '4px 0 0', paddingLeft: 20, fontSize: '0.85rem' }}>
                    {catalogNotFound.slice(0, 3).map((nf, i) => (
                      <li key={i}>{nf.ci_name} ({nf.ci})</li>
                    ))}
                    {catalogNotFound.length > 3 && <li>... and {catalogNotFound.length - 3} more</li>}
                  </ul>
                </div>
              )}
            </Alert>
          )}

          {/* WARNINGS (non-blocking) */}
          {(catalogNamespaceMismatches.length > 0 || usersNotInCatalog.filter(a => a.severity === 'high').length > 0 || warnings.length > 0) && (
            <Alert variant="warning" isInline title="Warnings detected" style={{ margin: '12px 0' }}>
              <p style={{ marginBottom: 8 }}>Review these issues before deploying:</p>
              {catalogNamespaceMismatches.length > 0 && (
                <div style={{ marginBottom: 8 }}>
                  <strong>• Catalog namespace mismatches ({catalogNamespaceMismatches.length}):</strong>
                  <ul style={{ margin: '4px 0 0', paddingLeft: 20, fontSize: '0.85rem' }}>
                    {catalogNamespaceMismatches.slice(0, 2).map((m, i) => (
                      <li key={i}>{m.ci_name}: expected {m.expected_catalog_namespace}, found in {m.found_catalog_namespace}</li>
                    ))}
                    {catalogNamespaceMismatches.length > 2 && <li>... and {catalogNamespaceMismatches.length - 2} more (may create ghost workshops)</li>}
                  </ul>
                </div>
              )}
              {usersNotInCatalog.filter(a => a.severity === 'high').length > 0 && (
                <div style={{ marginBottom: 8 }}>
                  <strong>• High-severity Users/Instances issues ({usersNotInCatalog.filter(a => a.severity === 'high').length}):</strong>
                  <ul style={{ margin: '4px 0 0', paddingLeft: 20, fontSize: '0.85rem' }}>
                    {usersNotInCatalog.filter(a => a.severity === 'high').slice(0, 2).map((a, i) => (
                      <li key={i}>{a.ci_name}: {a.message}</li>
                    ))}
                    {usersNotInCatalog.filter(a => a.severity === 'high').length > 2 && <li>... and {usersNotInCatalog.filter(a => a.severity === 'high').length - 2} more</li>}
                  </ul>
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
        </ModalBody>
        <ModalFooter>
          <Button
            variant="danger"
            onClick={handleDeploy}
            isDisabled={numUsersViolations.length > 0 || catalogNotFound.length > 0}
          >
            {numUsersViolations.length > 0 || catalogNotFound.length > 0 ? 'Cannot Deploy (blocked)' : 'Deploy Now'}
          </Button>
          <Button variant="link" onClick={() => setShowDeployConfirm(false)}>Cancel</Button>
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
                    showToast(`Done — ${parts.join(', ')}. Babylon will provision clusters (30–60 min).`, 'success');
                    // Do NOT re-validate here: pool CRD exists but has no ready clusters yet.
                    // The danger alert should stay until the pool is actually provisioned.
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
