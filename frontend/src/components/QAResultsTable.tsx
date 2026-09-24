import { useMemo, useState } from 'react';
import {
  Pagination,
  Title,
  ExpandableSection,
} from '@patternfly/react-core';
import { Table, Thead, Tbody, Tr, Th, Td, ThProps } from '@patternfly/react-table';

import { statusColorClass, statusIcon } from '../utils/statusColors';
import type { QAResult } from '../types';

function healthyDisplay(h: boolean | string | null | undefined): string {
  if (h === true || h === 'Yes') return 'Yes';
  if (h === false) return 'No';
  return String(h ?? '-');
}

function healthyColorClass(h: boolean | string | null | undefined): string {
  if (h === true || h === 'Yes') return 'status-verified';
  if (h === false || h === 'No') return 'status-failed';
  return '';
}

/** Expected seats from CSV vs live count; hide actual when not deployed to avoid misleading 10/10. */
function seatsDisplay(r: QAResult): string {
  const rec = r as QAResult & { expected_seats?: unknown; actual_seats?: unknown; actual_users?: unknown };
  const rawExp = rec.expected_users ?? rec.expected_seats;
  const exp =
    rawExp === null || rawExp === undefined || rawExp === '' ? '—' : String(rawExp);
  const deployedYes = String(r.deployed || '').trim().toLowerCase() === 'yes';
  if (!deployedYes) {
    return `${exp} / —`;
  }
  const rawAct = rec.actual_count ?? rec.actual_seats ?? rec.actual_users;
  const act =
    rawAct === null || rawAct === undefined || rawAct === '' ? '—' : String(rawAct);
  return `${exp} / ${act}`;
}

function issuesDisplay(r: QAResult): string {
  const raw = r.issues;
  if (raw === null || raw === undefined || raw === '') return '';
  return String(raw).trim();
}

type SortableQAColumn = 'ci_name' | 'ci' | 'status';

/** Extracted QA results table with sorting + pagination */
export const QAResultsTable: React.FC<{
  qaResults: QAResult[];
  title: string;
  page: number;
  setPage: (p: number) => void;
  perPage: number;
  setPerPage: (pp: number) => void;
  sortBy: SortableQAColumn | null;
  setSortBy: (c: SortableQAColumn) => void;
  sortDir: 'asc' | 'desc';
  setSortDir: (d: 'asc' | 'desc') => void;
  groupByNamespace?: boolean;
}> = ({ qaResults, title, page, setPage, perPage, setPerPage, sortBy, setSortBy, sortDir, setSortDir, groupByNamespace = false }) => {
  const sorted = useMemo(() => {
    if (!sortBy) return qaResults;
    return [...qaResults].sort((a, b) => {
      const aVal = (String(a[sortBy] || '')).toLowerCase();
      const bVal = (String(b[sortBy] || '')).toLowerCase();
      const cmp = aVal.localeCompare(bVal);
      return sortDir === 'asc' ? cmp : -cmp;
    });
  }, [qaResults, sortBy, sortDir]);

  const paginated = useMemo(() => {
    const start = (page - 1) * perPage;
    return sorted.slice(start, start + perPage);
  }, [sorted, page, perPage]);

  // Group results by namespace
  const groupedByNamespace = useMemo(() => {
    const groups: Record<string, QAResult[]> = {};
    for (const result of sorted) {
      const ns = result.namespace || '(no namespace)';
      if (!groups[ns]) groups[ns] = [];
      groups[ns].push(result);
    }
    return groups;
  }, [sorted]);

  const [expandedNamespaces, setExpandedNamespaces] = useState<Set<string>>(
    () => new Set(Object.keys(groupedByNamespace)),
  );

  const toggleNamespace = (ns: string) => {
    setExpandedNamespaces((prev) => {
      const next = new Set(prev);
      if (next.has(ns)) {
        next.delete(ns);
      } else {
        next.add(ns);
      }
      return next;
    });
  };

  const getSortParams = (col: SortableQAColumn): ThProps['sort'] => ({
    sortBy: sortBy === col ? { index: 0, direction: sortDir } : { index: 0, direction: 'asc', defaultDirection: 'asc' },
    onSort: () => {
      if (sortBy === col) {
        setSortDir(sortDir === 'asc' ? 'desc' : 'asc');
      } else {
        setSortBy(col);
        setSortDir('asc');
      }
      setPage(1);
    },
    columnIndex: 0,
  });

  const showShowroomCol = useMemo(
    () => qaResults.some((r) => r.showroom_status || r.showroom_url),
    [qaResults],
  );

  const renderTableRow = (r: QAResult) => {
    const issues = issuesDisplay(r);
    return (
      <Tr key={`${r.ci_name}-${r.ci}-${r.namespace}`}>
        <Td dataLabel="CI Name">
          <div>{r.ci_name}</div>
          <div className="qa-ci-meta" title={r.ci}>{r.ci}</div>
        </Td>
        {!groupByNamespace && <Td dataLabel="Namespace">{r.namespace || '-'}</Td>}
        <Td dataLabel="Status">
          <span className={statusColorClass(r.status)}>
            {(() => {
              const Icon = statusIcon(r.status);
              return Icon ? <Icon style={{ marginRight: 4 }} /> : null;
            })()}
            {r.status}
          </span>
        </Td>
        <Td dataLabel="Deployed">{r.deployed || '-'}</Td>
        <Td dataLabel="Healthy">
          <span className={healthyColorClass(r.healthy)}>{healthyDisplay(r.healthy)}</span>
        </Td>
        <Td dataLabel="Seats">{seatsDisplay(r)}</Td>
        <Td dataLabel="Issues">
          {issues ? (
            <span className="status-failed" title={issues} style={{ fontSize: '0.85rem' }}>
              {issues.length > 120 ? `${issues.slice(0, 117)}…` : issues}
            </span>
          ) : (
            '-'
          )}
        </Td>
        {showShowroomCol && (
          <Td dataLabel="Showroom">
            {r.showroom_status || r.showroom_url ? (
              <div>
                <span
                  className={
                    String(r.showroom_status).toLowerCase() === 'healthy'
                      ? 'status-verified'
                      : String(r.showroom_status).toLowerCase().includes('unhealthy') ||
                          String(r.showroom_status).toLowerCase() === 'error'
                        ? 'status-failed'
                        : ''
                  }
                >
                  {r.showroom_status || '—'}
                </span>
                {r.showroom_url ? (
                  <div>
                    <a
                      href={r.showroom_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="cell-truncate"
                      title={r.showroom_url}
                    >
                      {r.showroom_url}
                    </a>
                  </div>
                ) : null}
              </div>
            ) : (
              '-'
            )}
          </Td>
        )}
        <Td dataLabel="Landing Page URL">
          {r.landing_page_url ? (
            <a
              href={r.landing_page_url}
              target="_blank"
              rel="noopener noreferrer"
              className="cell-truncate"
              title={r.landing_page_url}
            >
              {r.landing_page_url}
            </a>
          ) : (
            '-'
          )}
        </Td>
      </Tr>
    );
  };

  const renderHeader = () => (
    <Tr>
      <Th sort={getSortParams('ci_name')} info={{ tooltip: 'Catalog Item display name' }}>
        CI Name
      </Th>
      {!groupByNamespace && (
        <Th info={{ tooltip: 'Namespace checked during QA' }}>Namespace</Th>
      )}
      <Th sort={getSortParams('status')} info={{ tooltip: 'QA verification result: verified or failed' }}>
        Status
      </Th>
      <Th info={{ tooltip: 'Whether the workshop was successfully deployed and running' }}>
        Deployed
      </Th>
      <Th info={{ tooltip: 'Whether the deployed workshop passed health checks' }}>Healthy</Th>
      <Th info={{ tooltip: 'Expected from CSV / actual provisioned seats when deployed' }}>
        Seats
      </Th>
      <Th info={{ tooltip: 'Mismatch details when status is failed' }}>Issues</Th>
      {showShowroomCol && (
        <Th info={{ tooltip: 'Soundcheck from QA3 — full batched run; click through for session detail. Admin Ops also has Run Soundcheck under Actions for ad-hoc batches.' }}>
          Showroom
        </Th>
      )}
      <Th info={{ tooltip: 'Student-facing URL — also on the Students tab' }}>
        Landing Page URL
      </Th>
    </Tr>
  );

  return (
    <>
      <Title headingLevel="h3" style={{ marginBottom: 8 }}>
        {title}
      </Title>

      {groupByNamespace ? (
        <>
          {Object.entries(groupedByNamespace).map(([ns, results]) => (
            <ExpandableSection
              key={ns}
              toggleText={`${ns} (${results.length} ${results.length === 1 ? 'workshop' : 'workshops'})`}
              isExpanded={expandedNamespaces.has(ns)}
              onToggle={() => toggleNamespace(ns)}
              style={{ marginBottom: 16 }}
            >
              <div className="table-sticky-wrapper">
                <Table aria-label={`QA results for ${ns}`} variant="compact" className="fixed-table">
                  <Thead>{renderHeader()}</Thead>
                  <Tbody>{results.map(renderTableRow)}</Tbody>
                </Table>
              </div>
            </ExpandableSection>
          ))}
        </>
      ) : (
        <div className="table-sticky-wrapper">
          <Table aria-label="QA results" variant="compact" className="fixed-table" isStickyHeader>
            <Thead>{renderHeader()}</Thead>
            <Tbody>{paginated.map(renderTableRow)}</Tbody>
          </Table>
        </div>
      )}
      {!groupByNamespace && sorted.length > perPage && (
        <Pagination
          itemCount={sorted.length}
          perPage={perPage}
          page={page}
          onSetPage={(_e, p) => setPage(p)}
          onPerPageSelect={(_e, pp) => {
            setPerPage(pp);
            setPage(1);
          }}
          perPageOptions={[
            { title: '10', value: 10 },
            { title: '20', value: 20 },
            { title: '50', value: 50 },
          ]}
          style={{ marginTop: 8 }}
        />
      )}
    </>
  );
};
