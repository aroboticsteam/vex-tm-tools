import { useEffect, useState } from 'react';
import { Pencil, Plus, Trash2 } from 'lucide-react';
import {
  useButtonPages,
  useCreateButton,
  useCreatePage,
  useDeleteButton,
  useDeletePage,
  usePressButton,
  useUpdateButton,
  useUpdatePage,
} from '../api/buttonPages';
import { usePermission } from '../hooks/usePermission';
import { useUiStore } from '../stores/ui';
import { Button } from '../components/ui/Button';
import { Card, PageHeader } from '../components/ui/Card';
import { Input, Label } from '../components/ui/Input';
import { Modal } from '../components/ui/Modal';
import { YamlEditor } from '../components/ui/YamlEditor';
import type { ButtonItem, ButtonPage } from '../types/api';

const DEFAULT_YAML = `- service: obs.switch_scene
  target: obs.main
  data:
    scene_name: Program
`;

/** Stream-deck-style pages of buttons. Each button runs a YAML action chain
 * (the same format as scripts/automations), so it can drive any integration
 * (OBS, ATEM, WLED, PTZ, OSC, Spotify, TM, ...) or call a script. */
export function ButtonPagesPage() {
  const { data: pages, isLoading } = useButtonPages();
  const canPress = usePermission('buttons:press');
  const canManage = usePermission('buttons:manage');
  const press = usePressButton();
  const deletePage = useDeletePage();
  const deleteButton = useDeleteButton();
  const pushToast = useUiStore((s) => s.pushToast);

  const [activeId, setActiveId] = useState<string | null>(null);
  const [editMode, setEditMode] = useState(false);
  const [pressing, setPressing] = useState<string | null>(null);
  const [editingButton, setEditingButton] = useState<ButtonItem | 'new' | null>(null);
  const [editingPage, setEditingPage] = useState<ButtonPage | 'new' | null>(null);

  const list = pages ?? [];
  const active = list.find((p) => p.id === activeId) ?? list[0] ?? null;
  useEffect(() => {
    if (active && active.id !== activeId) setActiveId(active.id);
  }, [active, activeId]);

  function onPress(button: ButtonItem) {
    if (editMode) {
      setEditingButton(button);
      return;
    }
    setPressing(button.id);
    press.mutate(button.id, {
      onSuccess: (result) => {
        if (result.status === 'success') pushToast(`${button.label}: done`, 'success');
        else pushToast(`${button.label} failed: ${result.error ?? 'unknown error'}`, 'error');
      },
      onError: (err) => pushToast(err instanceof Error ? err.message : 'Press failed', 'error'),
      onSettled: () => setPressing(null),
    });
  }

  function removeButton(button: ButtonItem) {
    if (!window.confirm(`Delete button "${button.label}"?`)) return;
    deleteButton.mutate(button.id, {
      onSuccess: () => {
        setEditingButton(null);
        pushToast('Button deleted', 'success');
      },
      onError: (err) => pushToast(err instanceof Error ? err.message : 'Delete failed', 'error'),
    });
  }

  function removePage(page: ButtonPage) {
    if (!window.confirm(`Delete page "${page.name}" and its ${page.buttons.length} buttons?`)) return;
    deletePage.mutate(page.id, {
      onSuccess: () => {
        setActiveId(null);
        pushToast('Page deleted', 'success');
      },
      onError: (err) => pushToast(err instanceof Error ? err.message : 'Delete failed', 'error'),
    });
  }

  return (
    <div>
      <PageHeader
        title="Button pages"
        subtitle="Pressable buttons that run actions across your integrations — like a stream deck, in the browser."
        action={
          canManage && (
            <div className="flex gap-2">
              <Button variant={editMode ? 'primary' : 'secondary'} onClick={() => setEditMode((v) => !v)}>
                <Pencil size={14} /> {editMode ? 'Done editing' : 'Edit'}
              </Button>
              <Button variant="secondary" onClick={() => setEditingPage('new')}>
                <Plus size={14} /> New page
              </Button>
            </div>
          )
        }
      />

      {isLoading && <p className="text-sm text-vmd-textSubtle">Loading…</p>}
      {!isLoading && list.length === 0 && (
        <p className="text-sm text-vmd-textSubtle">No pages yet.{canManage ? ' Create one to get started.' : ''}</p>
      )}

      {list.length > 0 && (
        <div className="mb-4 flex flex-wrap gap-1.5" role="tablist">
          {list.map((p) => (
            <button
              key={p.id}
              role="tab"
              aria-selected={p.id === active?.id}
              onClick={() => setActiveId(p.id)}
              className={`rounded-lg border px-3 py-1.5 text-sm ${
                p.id === active?.id
                  ? 'border-vmd-accent bg-vmd-surfaceSubtle text-vmd-textStrong'
                  : 'border-vmd-border text-vmd-textMuted hover:text-vmd-textStrong'
              }`}
            >
              {p.name}
            </button>
          ))}
        </div>
      )}

      {active && (
        <Card>
          {editMode && canManage && (
            <div className="mb-3 flex flex-wrap gap-2">
              <Button variant="secondary" onClick={() => setEditingPage(active)}>
                <Pencil size={14} /> Page settings
              </Button>
              <Button variant="secondary" onClick={() => setEditingButton('new')}>
                <Plus size={14} /> Add button
              </Button>
              <Button variant="ghost" onClick={() => removePage(active)}>
                <Trash2 size={14} /> Delete page
              </Button>
            </div>
          )}
          <div className="grid gap-3" style={{ gridTemplateColumns: `repeat(${Math.min(active.columns, 12)}, minmax(0, 1fr))` }}>
            {active.buttons.map((b) => (
              <button
                key={b.id}
                type="button"
                onClick={() => onPress(b)}
                disabled={(!canPress && !editMode) || pressing === b.id}
                style={{ backgroundColor: b.color }}
                className={`min-h-20 rounded-xl px-2 py-3 text-center text-sm font-semibold text-white shadow transition active:scale-95 disabled:opacity-50 ${
                  editMode ? 'outline outline-2 outline-dashed outline-white/50' : ''
                }`}
              >
                {b.label}
              </button>
            ))}
          </div>
          {active.buttons.length === 0 && <p className="text-sm text-vmd-textSubtle">No buttons on this page yet.</p>}
        </Card>
      )}

      <PageFormModal key={editingPage === null ? 'closed' : editingPage === 'new' ? 'new' : editingPage.id} editing={editingPage} onClose={() => setEditingPage(null)} />
      {active && (
        <ButtonFormModal
          key={editingButton === null ? 'closed' : editingButton === 'new' ? 'new' : editingButton.id}
          editing={editingButton}
          pageId={active.id}
          nextPosition={active.buttons.length}
          onClose={() => setEditingButton(null)}
          onDelete={removeButton}
        />
      )}
    </div>
  );
}

function PageFormModal({ editing, onClose }: { editing: ButtonPage | 'new' | null; onClose: () => void }) {
  const existing = editing && editing !== 'new' ? editing : null;
  const create = useCreatePage();
  const update = useUpdatePage();
  const pushToast = useUiStore((s) => s.pushToast);
  const [name, setName] = useState(existing?.name ?? '');
  const [columns, setColumns] = useState(existing?.columns ?? 4);

  function submit() {
    const body = { name: name.trim(), columns: Math.max(1, Math.min(12, Math.round(columns) || 4)), position: existing?.position ?? 0 };
    const callbacks = {
      onSuccess: () => {
        pushToast(existing ? 'Page updated' : 'Page created', 'success');
        onClose();
      },
      onError: (err: unknown) => pushToast(err instanceof Error ? err.message : 'Save failed', 'error'),
    };
    if (existing) update.mutate({ id: existing.id, body }, callbacks);
    else create.mutate(body, callbacks);
  }

  return (
    <Modal open={editing !== null} onClose={onClose} title={existing ? 'Page settings' : 'New page'}>
      <div className="space-y-3">
        <div>
          <Label>Name</Label>
          <Input value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div>
          <Label>Columns (1–12)</Label>
          <Input type="number" min={1} max={12} value={columns} onChange={(e) => setColumns(Number(e.target.value))} />
        </div>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" onClick={submit} disabled={!name.trim() || create.isPending || update.isPending}>
            Save
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function ButtonFormModal({
  editing,
  pageId,
  nextPosition,
  onClose,
  onDelete,
}: {
  editing: ButtonItem | 'new' | null;
  pageId: string;
  nextPosition: number;
  onClose: () => void;
  onDelete: (b: ButtonItem) => void;
}) {
  const existing = editing && editing !== 'new' ? editing : null;
  const create = useCreateButton();
  const update = useUpdateButton();
  const pushToast = useUiStore((s) => s.pushToast);
  const [label, setLabel] = useState(existing?.label ?? '');
  const [color, setColor] = useState(existing?.color ?? '#374151');
  const [yaml, setYaml] = useState(existing?.action_yaml ?? DEFAULT_YAML);

  function submit() {
    const body = { label: label.trim(), color, position: existing?.position ?? nextPosition, action_yaml: yaml };
    const callbacks = {
      onSuccess: () => {
        pushToast(existing ? 'Button updated' : 'Button created', 'success');
        onClose();
      },
      onError: (err: unknown) => pushToast(err instanceof Error ? err.message : 'Save failed', 'error'),
    };
    if (existing) update.mutate({ id: existing.id, body }, callbacks);
    else create.mutate({ pageId, body }, callbacks);
  }

  return (
    <Modal open={editing !== null} onClose={onClose} title={existing ? `Edit ${existing.label}` : 'New button'} wide>
      <div className="space-y-3">
        <div className="grid grid-cols-[1fr_auto] gap-3">
          <div>
            <Label>Label</Label>
            <Input value={label} onChange={(e) => setLabel(e.target.value)} />
          </div>
          <div>
            <Label>Colour</Label>
            <input type="color" value={color} onChange={(e) => setColor(e.target.value)} className="h-9 w-16 rounded" />
          </div>
        </div>
        <div>
          <Label>Actions (YAML — same format as scripts; list services, scripts, delays…)</Label>
          <YamlEditor value={yaml} onChange={setYaml} rows={10} />
        </div>
        <div className="flex justify-between gap-2">
          <div>
            {existing && (
              <Button variant="danger" onClick={() => onDelete(existing)}>
                <Trash2 size={14} /> Delete
              </Button>
            )}
          </div>
          <div className="flex gap-2">
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button variant="primary" onClick={submit} disabled={!label.trim() || create.isPending || update.isPending}>
              Save
            </Button>
          </div>
        </div>
      </div>
    </Modal>
  );
}
