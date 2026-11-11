import { useState } from 'react';
import { ChevronDown, ChevronRight, FolderPlus, History, Pencil, Play, Plus, Trash2 } from 'lucide-react';
import {
  useAutomationFolders,
  useAutomationRuns,
  useAutomations,
  useCreateAutomationFolder,
  useDeleteAutomation,
  useDeleteAutomationFolder,
  useTriggerAutomation,
  useUpdateAutomation,
  useUpdateAutomationFolder,
} from '../api/automations';
import { usePermission } from '../hooks/usePermission';
import { useUiStore } from '../stores/ui';
import { PageHeader, Card } from '../components/ui/Card';
import { Button } from '../components/ui/Button';
import { Toggle } from '../components/ui/Toggle';
import { AutomationEditorModal } from '../components/automations/AutomationEditorModal';
import type { Automation, AutomationFolder } from '../types/api';

export function AutomationsPage() {
  const { data: folders } = useAutomationFolders();
  const { data: automations, isLoading } = useAutomations();
  const createFolder = useCreateAutomationFolder();
  const updateFolder = useUpdateAutomationFolder();
  const deleteFolder = useDeleteAutomationFolder();
  const deleteAutomation = useDeleteAutomation();
  const updateAutomation = useUpdateAutomation();
  const triggerAutomation = useTriggerAutomation();
  const canEdit = usePermission('automations:edit');
  const canTrigger = usePermission('automations:trigger');
  const pushToast = useUiStore((s) => s.pushToast);

  const [selectedFolder, setSelectedFolder] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<Automation | null>(null);
  const [expandedHistory, setExpandedHistory] = useState<string | null>(null);

  const filtered = (automations ?? []).filter((a) =>
    selectedFolder === null ? true : a.folder_id === selectedFolder,
  );

  function handleRenameFolder(folder: AutomationFolder) {
    const name = window.prompt('Rename folder', folder.name);
    if (!name || name === folder.name) return;
    updateFolder.mutate(
      { id: folder.id, body: { name } },
      {
        onSuccess: () => pushToast('Folder renamed', 'success'),
        onError: (err) => pushToast(err instanceof Error ? err.message : 'Failed to rename folder', 'error'),
      },
    );
  }

  function handleDeleteFolder(folder: AutomationFolder) {
    if (
      !window.confirm(
        `Delete folder "${folder.name}"? Automations inside it are not deleted, but will become unfiled.`,
      )
    ) {
      return;
    }
    deleteFolder.mutate(folder.id, {
      onSuccess: () => {
        pushToast('Folder deleted', 'success');
        if (selectedFolder === folder.id) setSelectedFolder(null);
      },
      onError: (err) => pushToast(err instanceof Error ? err.message : 'Failed to delete folder', 'error'),
    });
  }

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-[220px_1fr]">
      <aside>
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">Folders</h2>
          {canEdit && (
            <button
              onClick={() => {
                const name = window.prompt('Folder name');
                if (name) createFolder.mutate({ name });
              }}
              className="text-vmd-textSubtle hover:text-vmd-textStrong"
            >
              <FolderPlus size={16} />
            </button>
          )}
        </div>
        <ul className="space-y-1 text-sm">
          <li>
            <button
              onClick={() => setSelectedFolder(null)}
              className={`w-full rounded-lg px-2 py-1 text-left ${selectedFolder === null ? 'bg-vmd-surfaceSubtle text-vmd-textStrong' : 'text-vmd-textMuted'}`}
            >
              All automations
            </button>
          </li>
          {folders?.map((f) => (
            <li key={f.id} className="group flex items-center gap-1">
              <button
                onClick={() => setSelectedFolder(f.id)}
                className={`flex-1 rounded-lg px-2 py-1 text-left ${selectedFolder === f.id ? 'bg-vmd-surfaceSubtle text-vmd-textStrong' : 'text-vmd-textMuted'}`}
              >
                {f.name}
              </button>
              {canEdit && (
                <span className="flex items-center gap-1 opacity-0 group-hover:opacity-100">
                  <button
                    onClick={() => handleRenameFolder(f)}
                    className="text-vmd-textSubtle hover:text-vmd-textStrong"
                    title="Rename folder"
                  >
                    <Pencil size={12} />
                  </button>
                  <button
                    onClick={() => handleDeleteFolder(f)}
                    className="text-vmd-textSubtle hover:text-vmd-danger"
                    title="Delete folder"
                  >
                    <Trash2 size={12} />
                  </button>
                </span>
              )}
            </li>
          ))}
        </ul>
      </aside>

      <div>
        <PageHeader
          title="Automations"
          subtitle="YAML/Jinja2 trigger-condition-action rules."
          action={
            canEdit && (
              <Button
                variant="primary"
                onClick={() => {
                  setEditing(null);
                  setModalOpen(true);
                }}
              >
                <Plus size={16} /> New Automation
              </Button>
            )
          }
        />

        {isLoading && <p className="text-sm text-vmd-textSubtle">Loading…</p>}
        <div className="space-y-3">
          {filtered.map((automation) => (
            <Card key={automation.id}>
              <div className="flex items-center justify-between">
                <div>
                  <p className="font-medium text-vmd-textStrong">{automation.alias}</p>
                  <p className="text-xs text-vmd-textSubtle">
                    {automation.last_triggered_at
                      ? `Last triggered ${new Date(automation.last_triggered_at).toLocaleString()}`
                      : 'Never triggered'}
                  </p>
                </div>
                <div className="flex items-center gap-3">
                  <Toggle
                    checked={automation.enabled}
                    disabled={!canEdit}
                    onChange={(enabled) => updateAutomation.mutate({ id: automation.id, body: { enabled } })}
                  />
                  {canTrigger && (
                    <button
                      onClick={() =>
                        triggerAutomation.mutate(automation.id, {
                          onSuccess: (res) => pushToast(`Test run: ${res.status}`, res.status === 'success' ? 'success' : 'error'),
                        })
                      }
                      className="text-vmd-textSubtle hover:text-vmd-textStrong"
                      title="Test Run"
                    >
                      <Play size={16} />
                    </button>
                  )}
                  <button
                    onClick={() =>
                      setExpandedHistory((cur) => (cur === automation.id ? null : automation.id))
                    }
                    className="flex items-center gap-1 text-vmd-textSubtle hover:text-vmd-textStrong"
                    title="Execution History"
                  >
                    <History size={16} />
                    {expandedHistory === automation.id ? (
                      <ChevronDown size={14} />
                    ) : (
                      <ChevronRight size={14} />
                    )}
                  </button>
                  <button
                    onClick={() => {
                      setEditing(automation);
                      setModalOpen(true);
                    }}
                    className="text-sm text-vmd-link hover:underline"
                  >
                    Edit
                  </button>
                  {canEdit && (
                    <button
                      onClick={() => deleteAutomation.mutate(automation.id)}
                      className="text-vmd-textSubtle hover:text-vmd-danger"
                    >
                      <Trash2 size={16} />
                    </button>
                  )}
                </div>
              </div>

              {expandedHistory === automation.id && <AutomationHistoryPanel automationId={automation.id} />}
            </Card>
          ))}
          {filtered.length === 0 && !isLoading && (
            <p className="text-sm text-vmd-textSubtle">No automations in this folder yet.</p>
          )}
        </div>
      </div>

      <AutomationEditorModal open={modalOpen} onClose={() => setModalOpen(false)} editing={editing} />
    </div>
  );
}

/**
 * Plan Appendix A.7 / audit finding 4.2: the backend has always written
 * `AutomationRun` rows and exposed `GET /{id}/runs`, but no frontend view
 * ever consumed `useAutomationRuns()`. This panel renders it as a simple
 * table: when it ran, a summary of the trigger event, success/failure, and
 * which action index failed (if any).
 */
function AutomationHistoryPanel({ automationId }: { automationId: string }) {
  const { data: runs, isLoading } = useAutomationRuns(automationId);

  return (
    <div className="mt-3 border-t border-vmd-border pt-3">
      {isLoading && <p className="text-sm text-vmd-textSubtle">Loading history…</p>}
      {!isLoading && (!runs || runs.length === 0) && (
        <p className="text-sm text-vmd-textSubtle">No executions recorded yet.</p>
      )}
      {runs && runs.length > 0 && (
        <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase text-vmd-textSubtle">
              <th className="pb-2">Triggered At</th>
              <th className="pb-2">Trigger Event</th>
              <th className="pb-2">Status</th>
              <th className="pb-2">Failed Action</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.id} className="border-t border-vmd-border align-top">
                <td className="py-1.5 whitespace-nowrap">{new Date(run.triggered_at).toLocaleString()}</td>
                <td className="py-1.5 max-w-xs truncate text-vmd-textMuted" title={JSON.stringify(run.trigger_event)}>
                  {run.trigger_event ? JSON.stringify(run.trigger_event) : '—'}
                </td>
                <td className="py-1.5">
                  <span
                    className={run.status === 'success' ? 'text-vmd-success' : 'text-vmd-danger'}
                  >
                    {run.status}
                  </span>
                </td>
                <td className="py-1.5">
                  {run.failed_action_index !== null ? (
                    <span title={run.error ?? undefined}>
                      #{run.failed_action_index}
                      {run.error ? ` — ${run.error}` : ''}
                    </span>
                  ) : (
                    '—'
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
    </div>
  );
}
