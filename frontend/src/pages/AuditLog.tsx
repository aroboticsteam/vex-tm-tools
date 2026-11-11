import { useState } from 'react';
import { useAuditLog } from '../api/audit';
import { PageHeader, Card } from '../components/ui/Card';
import { Input } from '../components/ui/Input';
import { Button } from '../components/ui/Button';
import type { AuditLogFilters } from '../types/api';

const PAGE_SIZE = 50;

/**
 * Real `GET /api/v1/audit` view (plan §11/§12, audit findings 1.5/1.6).
 * Per Appendix C.8: audit history is REST-owned (TanStack Query via
 * `useAuditLog`) — this page does not read live data from the WS store at
 * all. A later wave may wire a WS-triggered `invalidateQueries(['audit'])`
 * via `useCacheSync` for near-real-time updates; until then, a manual
 * "Refresh" button covers it without violating the REST/WS separation rule.
 */
export function AuditLogPage() {
  const [filters, setFilters] = useState<AuditLogFilters>({ limit: PAGE_SIZE, offset: 0 });
  const { data: entries, isLoading, isError, refetch, isFetching } = useAuditLog(filters);

  function updateFilter<K extends keyof AuditLogFilters>(key: K, value: AuditLogFilters[K]) {
    setFilters((prev) => ({ ...prev, [key]: value || undefined, offset: 0 }));
  }

  return (
    <div>
      <PageHeader title="Audit Log" subtitle="Create/update/delete operations across the system." />

      <Card className="mb-4">
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label className="mb-1 block text-xs uppercase text-vmd-textSubtle">User</label>
            <Input
              placeholder="user id"
              value={filters.user ?? ''}
              onChange={(e) => updateFilter('user', e.target.value)}
              className="w-40"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs uppercase text-vmd-textSubtle">Action</label>
            <select
              value={filters.action ?? ''}
              onChange={(e) => updateFilter('action', e.target.value)}
              className="rounded-full border border-vmd-border bg-vmd-surfaceSubtle px-3 py-1.5 text-sm text-vmd-text"
            >
              <option value="">All</option>
              <option value="create">create</option>
              <option value="update">update</option>
              <option value="delete">delete</option>
              <option value="trigger">trigger</option>
              <option value="service_call">service_call</option>
            </select>
          </div>
          <div>
            <label className="mb-1 block text-xs uppercase text-vmd-textSubtle">Resource type</label>
            <Input
              placeholder="e.g. integration"
              value={filters.resource_type ?? ''}
              onChange={(e) => updateFilter('resource_type', e.target.value)}
              className="w-40"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs uppercase text-vmd-textSubtle">From</label>
            <Input
              type="datetime-local"
              value={filters.start_date ?? ''}
              onChange={(e) => updateFilter('start_date', e.target.value)}
            />
          </div>
          <div>
            <label className="mb-1 block text-xs uppercase text-vmd-textSubtle">To</label>
            <Input
              type="datetime-local"
              value={filters.end_date ?? ''}
              onChange={(e) => updateFilter('end_date', e.target.value)}
            />
          </div>
          <Button variant="secondary" onClick={() => void refetch()} disabled={isFetching}>
            {isFetching ? 'Refreshing…' : 'Refresh'}
          </Button>
        </div>
      </Card>

      <Card>
        {isLoading ? (
          <p className="text-sm text-vmd-textSubtle">Loading…</p>
        ) : isError ? (
          <p className="text-sm text-vmd-danger">Failed to load audit log.</p>
        ) : !entries || entries.length === 0 ? (
          <p className="text-sm text-vmd-textSubtle">No audit entries match these filters.</p>
        ) : (
          <>
            <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs uppercase text-vmd-textSubtle">
                  <th className="pb-2">Time</th>
                  <th className="pb-2">User</th>
                  <th className="pb-2">Action</th>
                  <th className="pb-2">Resource</th>
                  <th className="pb-2">Changes</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((entry) => (
                  <tr key={entry.id} className="border-t border-vmd-border align-top">
                    <td className="py-1.5 whitespace-nowrap">
                      {new Date(entry.created_at).toLocaleString()}
                    </td>
                    <td className="py-1.5">{entry.user_id ?? '—'}</td>
                    <td className="py-1.5">{entry.action}</td>
                    <td className="py-1.5">
                      {entry.resource_type ?? '—'} / {entry.resource_id ?? '—'}
                    </td>
                    <td className="py-1.5 max-w-md">
                      {entry.changes ? (
                        <pre className="whitespace-pre-wrap break-all text-xs text-vmd-textMuted">
                          {JSON.stringify(entry.changes)}
                        </pre>
                      ) : (
                        '—'
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
            <div className="mt-3 flex items-center justify-between">
              <span className="text-xs text-vmd-textSubtle">
                Showing {(filters.offset ?? 0) + 1}–{(filters.offset ?? 0) + entries.length}
              </span>
              <div className="flex gap-2">
                <Button
                  variant="ghost"
                  disabled={(filters.offset ?? 0) === 0}
                  onClick={() =>
                    setFilters((prev) => ({ ...prev, offset: Math.max(0, (prev.offset ?? 0) - PAGE_SIZE) }))
                  }
                >
                  Previous
                </Button>
                <Button
                  variant="ghost"
                  disabled={entries.length < PAGE_SIZE}
                  onClick={() =>
                    setFilters((prev) => ({ ...prev, offset: (prev.offset ?? 0) + PAGE_SIZE }))
                  }
                >
                  Next
                </Button>
              </div>
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
