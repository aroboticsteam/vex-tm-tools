import { useState } from 'react';
import { Check, ExternalLink, GripVertical, KeyRound, Pencil, Plus, RefreshCw, Send, ShieldCheck, ShieldOff, Trash2, Upload, Zap } from 'lucide-react';
import {
  fetchStationLink,
  useAckMessage,
  useAgenda,
  useDeleteAgendaItem,
  useDeletePreset,
  useDeleteStation,
  useImportAgenda,
  useInbox,
  usePresets,
  usePushScreen,
  useReorderAgenda,
  useRotateLink,
  useSaveAgendaItem,
  useSavePreset,
  useSaveStation,
  useSendMessage,
  useSetPin,
  useStations,
  useUnsecureStation,
  type Audience,
} from '../api/comms';
import { usePermission } from '../hooks/usePermission';
import { useUiStore } from '../stores/ui';
import { Button } from '../components/ui/Button';
import { Card, PageHeader } from '../components/ui/Card';
import { Input, Label, Select } from '../components/ui/Input';
import { Modal } from '../components/ui/Modal';
import { Toggle } from '../components/ui/Toggle';
import { TimerCard } from '../components/comms/TimerCard';
import { moveItem } from '../lib/actionBuilder';
import type { AgendaItem, CommsPreset, CommsScreen, Station, StationRole } from '../types/api';

const ROLE_LABEL: Record<StationRole, string> = { emcee: 'Emcees', floor_manager: 'Floor managers', camera: 'Camera companions' };
const SCREEN_LABEL: Record<CommsScreen, string> = {
  messages: 'Messages',
  countdown: 'Next match countdown (emcees)',
  timer: 'Timer',
  agenda_day: 'Day agenda',
  agenda_finals: 'Finals agenda',
};
const SLUG_PATTERN = /^[a-z0-9][a-z0-9-]{0,49}$/;
const TIME_PATTERN = /^([01]\d|2[0-3]):[0-5]\d$/;

function errorToast(push: (m: string, k: 'error') => void) {
  return (err: unknown) => push(err instanceof Error ? err.message : 'Request failed', 'error');
}

/** Desk operator console for emcee / floor manager / camera iPads:
 * send messages (optionally flashing), push screens, read replies, and
 * manage stations, canned messages and the agendas. */
export function CommsPage() {
  const canSend = usePermission('comms:send');
  const canManage = usePermission('comms:manage');
  const { data: stations = [] } = useStations();
  const { data: presets = [] } = usePresets();
  const { data: inbox = [] } = useInbox();
  const ack = useAckMessage();
  const send = useSendMessage();
  const pushScreen = usePushScreen();
  const pushToast = useUiStore((s) => s.pushToast);
  const toastErr = errorToast(pushToast);

  const [target, setTarget] = useState<string>('all'); // "all" | "role:<role>" | "station:<slug>"
  const [text, setText] = useState('');
  const [flash, setFlash] = useState(false);
  const [editStation, setEditStation] = useState<Station | 'new' | null>(null);
  const [editPreset, setEditPreset] = useState<CommsPreset | 'new' | null>(null);

  const audience: Audience =
    target === 'all' ? {} : target.startsWith('role:') ? { roles: [target.slice(5) as StationRole] } : { stations: [target.slice(8)] };
  const targetRole = target.startsWith('role:') ? target.slice(5) : stations.find((s) => `station:${s.slug}` === target)?.role;
  const offered = presets.filter((p) => !p.role || !targetRole || p.role === targetRole);

  function sendNow(message: string, flashing: boolean) {
    send.mutate(
      { ...audience, text: message, flash: flashing },
      { onSuccess: (r) => pushToast(`Sent to ${r.stations.length} station${r.stations.length === 1 ? '' : 's'}`, 'success'), onError: toastErr },
    );
  }

  return (
    <div>
      <PageHeader title="Comms" subtitle="Messages and screens for emcee, floor manager and camera iPads. Voice runs over Mumble (see docs/COMMS.md)." />

      {canSend && (
        <Card className="mb-4">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">Send</h2>
          <div className="grid gap-3 md:grid-cols-2">
            <div>
              <Label>To</Label>
              <Select value={target} onChange={(e) => setTarget(e.target.value)}>
                <option value="all">All stations</option>
                {(Object.keys(ROLE_LABEL) as StationRole[]).map((r) => (
                  <option key={r} value={`role:${r}`}>{ROLE_LABEL[r]}</option>
                ))}
                {stations.map((s) => (
                  <option key={s.slug} value={`station:${s.slug}`}>{s.name}</option>
                ))}
              </Select>
            </div>
            <div>
              <Label>Show screen</Label>
              <div className="flex flex-wrap gap-1.5">
                {(Object.keys(SCREEN_LABEL) as CommsScreen[]).map((s) => (
                  <Button
                    key={s}
                    variant="secondary"
                    onClick={() =>
                      pushScreen.mutate({ ...audience, screen: s }, { onSuccess: (r) => pushToast(`${SCREEN_LABEL[s]} → ${r.stations.length} station(s)`, 'success'), onError: toastErr })
                    }
                  >
                    {SCREEN_LABEL[s]}
                  </Button>
                ))}
              </div>
            </div>
          </div>
          {offered.length > 0 && (
            <div className="mt-3 flex flex-wrap gap-1.5">
              {offered.map((p) => (
                <Button key={p.id} variant="secondary" onClick={() => sendNow(p.message, p.flash)} title={p.message}>
                  {p.flash && <Zap size={14} />} {p.label}
                </Button>
              ))}
            </div>
          )}
          <form
            className="mt-3 flex flex-wrap items-center gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (!text.trim()) return;
              sendNow(text.trim(), flash);
              setText('');
            }}
          >
            <Input className="min-w-64 flex-1" placeholder="Custom message…" maxLength={500} value={text} onChange={(e) => setText(e.target.value)} />
            <label className="flex items-center gap-2 text-sm text-vmd-textMuted">
              <Toggle checked={flash} onChange={setFlash} /> Flash
            </label>
            <Button variant="primary" type="submit" disabled={!text.trim() || send.isPending}>
              <Send size={14} /> Send
            </Button>
          </form>
        </Card>
      )}

      {canSend && <TimerCard audience={audience} />}

      <Card className="mb-4">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">Messages from stations</h2>
        {inbox.length === 0 && <p className="text-sm text-vmd-textSubtle">Nothing waiting.</p>}
        {inbox.map((m) => (
          <div key={m.id} className="flex items-center justify-between gap-3 border-b border-vmd-border py-2 last:border-0">
            <div>
              <span className="mr-2 text-xs text-vmd-textSubtle">{stations.find((s) => s.slug === m.station_slug)?.name ?? m.station_slug}</span>
              <span className="text-sm text-vmd-textStrong">{m.text}</span>
            </div>
            {canSend && (
              <Button variant="ghost" onClick={() => ack.mutate(m.id, { onError: toastErr })} title="Mark as read">
                <Check size={14} />
              </Button>
            )}
          </div>
        ))}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <StationsCard stations={stations} canManage={canManage} onEdit={setEditStation} />
        <PresetsCard presets={presets} canManage={canManage} onEdit={setEditPreset} />
      </div>
      <AgendaCard canManage={canManage} />

      <StationModal key={editStation === null ? 'closed' : editStation === 'new' ? 'new' : editStation.slug} editing={editStation} onClose={() => setEditStation(null)} />
      <PresetModal key={editPreset === null ? 'pclosed' : editPreset === 'new' ? 'pnew' : editPreset.id} editing={editPreset} onClose={() => setEditPreset(null)} />
    </div>
  );
}

function lastSeenLabel(station: Station): string {
  if (station.connected) return 'Connected';
  if (station.last_seen == null) return 'Never opened';
  const mins = Math.max(0, Math.round((Date.now() / 1000 - station.last_seen) / 60));
  return mins < 1 ? 'Seen just now' : mins < 60 ? `Seen ${mins} min ago` : `Seen ${Math.round(mins / 60)} h ago`;
}

function StationsCard({ stations, canManage, onEdit }: { stations: Station[]; canManage: boolean; onEdit: (s: Station | 'new') => void }) {
  const del = useDeleteStation();
  const rotate = useRotateLink();
  const setPin = useSetPin();
  const unsecure = useUnsecureStation();
  const pushToast = useUiStore((s) => s.pushToast);
  const toastErr = errorToast(pushToast);

  async function copyLink(s: Station) {
    try {
      const path = s.secured ? (await fetchStationLink(s.slug)).path : `/station/${s.slug}`;
      await navigator.clipboard.writeText(`${window.location.origin}${path}`);
      pushToast(`Link for ${s.name} copied`, 'success');
    } catch (err) {
      toastErr(err);
    }
  }

  function rotateLink(s: Station) {
    const warning = s.secured
      ? `Make a new link for "${s.name}"? The old link stops working on every iPad using it.`
      : `Secure "${s.name}"? Its plain link will stop working; you'll copy a new private link.`;
    if (!window.confirm(warning)) return;
    rotate.mutate(s.slug, {
      onSuccess: async (link) => {
        try {
          await navigator.clipboard.writeText(`${window.location.origin}${link.path}`);
          pushToast('New link copied — open it on the iPad', 'success');
        } catch {
          pushToast('New link created — use Copy link', 'success');
        }
      },
      onError: toastErr,
    });
  }

  function changePin(s: Station) {
    const entered = window.prompt(`PIN for "${s.name}" (4–8 digits). Leave empty to remove the PIN.`, '');
    if (entered === null) return;
    const pin = entered.trim();
    if (pin !== '' && !/^\d{4,8}$/.test(pin)) {
      pushToast('A PIN is 4 to 8 digits', 'error');
      return;
    }
    setPin.mutate({ slug: s.slug, pin: pin === '' ? null : pin }, { onSuccess: () => pushToast(pin ? 'PIN set' : 'PIN removed', 'success'), onError: toastErr });
  }

  return (
    <Card>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">Stations (iPads)</h2>
        {canManage && (
          <Button variant="secondary" onClick={() => onEdit('new')}>
            <Plus size={14} /> Add
          </Button>
        )}
      </div>
      {stations.length === 0 && <p className="text-sm text-vmd-textSubtle">No stations yet. Add one per iPad, then open its link on that iPad.</p>}
      {stations.map((s) => (
        <div key={s.slug} data-testid="station-row" className="flex flex-wrap items-center justify-between gap-2 border-b border-vmd-border py-2 last:border-0">
          <div>
            <p className="flex items-center gap-2 text-sm font-medium text-vmd-textStrong">
              <span
                data-testid="presence"
                data-connected={s.connected}
                title={lastSeenLabel(s)}
                className={`inline-block h-2.5 w-2.5 rounded-full ${s.connected ? 'bg-vmd-success' : 'bg-vmd-textSubtle'}`}
              />
              {s.name}
              {s.secured && (
                <span className="inline-flex items-center gap-1 text-xs font-normal text-vmd-textMuted">
                  <ShieldCheck size={12} /> {s.has_pin ? 'Key + PIN' : 'Private link'}
                </span>
              )}
            </p>
            <p className="text-xs text-vmd-textSubtle">
              {ROLE_LABEL[s.role]} · /station/{s.slug} · {lastSeenLabel(s)}
            </p>
          </div>
          <div className="flex flex-wrap gap-1">
            {canManage || !s.secured ? (
              <Button variant="ghost" type="button" onClick={() => copyLink(s)} title="Copy the link to open on the iPad">
                Copy link
              </Button>
            ) : null}
            {!s.secured && (
              <a href={`/station/${s.slug}`} target="_blank" rel="noreferrer">
                <Button variant="ghost" type="button" title="Open"><ExternalLink size={14} /></Button>
              </a>
            )}
            {canManage && (
              <>
                <Button variant="ghost" onClick={() => rotateLink(s)} title={s.secured ? 'Rotate link (revokes the old one)' : 'Secure with a private link'}>
                  <RefreshCw size={14} /> {s.secured ? 'Rotate' : 'Secure'}
                </Button>
                <Button variant="ghost" onClick={() => changePin(s)} title="Set or remove the PIN"><KeyRound size={14} /></Button>
                {s.secured && (
                  <Button
                    variant="ghost"
                    title="Remove protection (open link works again)"
                    onClick={() => window.confirm(`Remove the private link and PIN from "${s.name}"?`) && unsecure.mutate(s.slug, { onError: toastErr })}
                  >
                    <ShieldOff size={14} />
                  </Button>
                )}
                <Button variant="ghost" onClick={() => onEdit(s)} title="Edit"><Pencil size={14} /></Button>
                <Button
                  variant="ghost"
                  title="Delete"
                  onClick={() => window.confirm(`Delete station "${s.name}"?`) && del.mutate(s.slug, { onError: toastErr })}
                >
                  <Trash2 size={14} />
                </Button>
              </>
            )}
          </div>
        </div>
      ))}
    </Card>
  );
}

function StationModal({ editing, onClose }: { editing: Station | 'new' | null; onClose: () => void }) {
  const existing = editing && editing !== 'new' ? editing : null;
  const save = useSaveStation();
  const pushToast = useUiStore((s) => s.pushToast);
  const [slug, setSlug] = useState(existing?.slug ?? '');
  const [name, setName] = useState(existing?.name ?? '');
  const [role, setRole] = useState<StationRole>(existing?.role ?? 'emcee');
  const slugOk = existing !== null || SLUG_PATTERN.test(slug);
  return (
    <Modal open={editing !== null} onClose={onClose} title={existing ? `Edit ${existing.name}` : 'New station'}>
      <div className="space-y-3">
        {!existing && (
          <div>
            <Label>URL name (lowercase letters, numbers, dashes)</Label>
            <Input placeholder="emcee-main" value={slug} onChange={(e) => setSlug(e.target.value.toLowerCase())} />
            {slug && !slugOk && <p className="mt-1 text-xs text-vmd-danger">Use a-z, 0-9 and dashes only.</p>}
          </div>
        )}
        <div><Label>Name</Label><Input value={name} onChange={(e) => setName(e.target.value)} /></div>
        <div>
          <Label>Role</Label>
          <Select value={role} onChange={(e) => setRole(e.target.value as StationRole)}>
            {(Object.keys(ROLE_LABEL) as StationRole[]).map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
          </Select>
        </div>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            disabled={!name.trim() || !slugOk || (!existing && !slug) || save.isPending}
            onClick={() =>
              save.mutate({ slug: existing?.slug ?? slug, body: { name: name.trim(), role }, isNew: !existing }, { onSuccess: onClose, onError: errorToast(pushToast) })
            }
          >
            Save
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function PresetsCard({ presets, canManage, onEdit }: { presets: CommsPreset[]; canManage: boolean; onEdit: (p: CommsPreset | 'new') => void }) {
  const del = useDeletePreset();
  const pushToast = useUiStore((s) => s.pushToast);
  return (
    <Card>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">Preset messages</h2>
        {canManage && (
          <Button variant="secondary" onClick={() => onEdit('new')}>
            <Plus size={14} /> Add
          </Button>
        )}
      </div>
      {presets.length === 0 && <p className="text-sm text-vmd-textSubtle">No presets yet.</p>}
      {presets.map((p) => (
        <div key={p.id} className="flex items-center justify-between gap-2 border-b border-vmd-border py-2 last:border-0">
          <div>
            <p className="text-sm font-medium text-vmd-textStrong">{p.flash && <Zap size={12} className="mr-1 inline" />}{p.label}</p>
            <p className="text-xs text-vmd-textSubtle">{p.message}{p.role ? ` · ${ROLE_LABEL[p.role]} only` : ''}</p>
          </div>
          {canManage && (
            <div className="flex gap-1">
              <Button variant="ghost" onClick={() => onEdit(p)} title="Edit"><Pencil size={14} /></Button>
              <Button variant="ghost" title="Delete" onClick={() => del.mutate(p.id, { onError: errorToast(pushToast) })}><Trash2 size={14} /></Button>
            </div>
          )}
        </div>
      ))}
    </Card>
  );
}

function PresetModal({ editing, onClose }: { editing: CommsPreset | 'new' | null; onClose: () => void }) {
  const existing = editing && editing !== 'new' ? editing : null;
  const save = useSavePreset();
  const pushToast = useUiStore((s) => s.pushToast);
  const [label, setLabel] = useState(existing?.label ?? '');
  const [message, setMessage] = useState(existing?.message ?? '');
  const [flash, setFlash] = useState(existing?.flash ?? false);
  const [role, setRole] = useState<string>(existing?.role ?? '');
  return (
    <Modal open={editing !== null} onClose={onClose} title={existing ? 'Edit preset' : 'New preset'}>
      <div className="space-y-3">
        <div><Label>Button label</Label><Input value={label} maxLength={60} onChange={(e) => setLabel(e.target.value)} /></div>
        <div><Label>Message</Label><Input value={message} maxLength={500} onChange={(e) => setMessage(e.target.value)} /></div>
        <div>
          <Label>Only for</Label>
          <Select value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="">Everyone</option>
            {(Object.keys(ROLE_LABEL) as StationRole[]).map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
          </Select>
        </div>
        <label className="flex items-center gap-2 text-sm text-vmd-textMuted"><Toggle checked={flash} onChange={setFlash} /> Flash to get attention</label>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            disabled={!label.trim() || !message.trim() || save.isPending}
            onClick={() =>
              save.mutate(
                { id: existing?.id, body: { label: label.trim(), message: message.trim(), flash, role: (role || null) as StationRole | null, position: existing?.position ?? 0 } },
                { onSuccess: onClose, onError: errorToast(pushToast) },
              )
            }
          >
            Save
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function AgendaCard({ canManage }: { canManage: boolean }) {
  const { data: items = [] } = useAgenda();
  const save = useSaveAgendaItem();
  const del = useDeleteAgendaItem();
  const reorder = useReorderAgenda();
  const pushToast = useUiStore((s) => s.pushToast);
  const [scope, setScope] = useState<'day' | 'finals'>('day');
  const [editing, setEditing] = useState<AgendaItem | 'new' | null>(null);
  const [importing, setImporting] = useState(false);
  const [dragId, setDragId] = useState<string | null>(null);
  const shown = items.filter((i) => i.scope === scope);

  function drop(targetId: string) {
    if (!dragId || dragId === targetId) return;
    const ids = shown.map((i) => i.id);
    reorder.mutate({ scope, ids: moveItem(ids, ids.indexOf(dragId), ids.indexOf(targetId)) }, { onError: errorToast(pushToast) });
  }

  return (
    <Card className="mt-4">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex gap-1.5">
          {(['day', 'finals'] as const).map((s) => (
            <Button key={s} variant={scope === s ? 'primary' : 'secondary'} onClick={() => setScope(s)}>
              {s === 'day' ? 'Day agenda' : 'Finals agenda'}
            </Button>
          ))}
        </div>
        {canManage && (
          <div className="flex gap-1.5">
            <Button variant="secondary" onClick={() => setImporting(true)}>
              <Upload size={14} /> Import
            </Button>
            <Button variant="secondary" onClick={() => setEditing('new')}>
              <Plus size={14} /> Add item
            </Button>
          </div>
        )}
      </div>
      {shown.length === 0 && <p className="text-sm text-vmd-textSubtle">No items.</p>}
      {canManage && shown.length > 1 && <p className="mb-1 text-xs text-vmd-textSubtle">Drag rows to reorder.</p>}
      {shown.map((i) => (
        <div
          key={i.id}
          data-testid="agenda-row"
          draggable={canManage}
          onDragStart={() => setDragId(i.id)}
          onDragOver={(e) => canManage && e.preventDefault()}
          onDrop={() => {
            drop(i.id);
            setDragId(null);
          }}
          className="flex items-center justify-between gap-2 border-b border-vmd-border py-2 last:border-0"
        >
          <div className="flex items-center gap-2">
            {canManage && <GripVertical size={14} className="cursor-grab text-vmd-textSubtle" aria-hidden />}
            <div>
              <p className="text-sm text-vmd-textStrong"><span className="mr-3 tabular-nums text-vmd-textMuted">{i.start_time}{i.end_time ? `–${i.end_time}` : ''}</span>{i.title}</p>
              {i.details && <p className="text-xs text-vmd-textSubtle">{i.details}</p>}
            </div>
          </div>
          {canManage && (
            <div className="flex gap-1">
              <Button variant="ghost" onClick={() => setEditing(i)} title="Edit"><Pencil size={14} /></Button>
              <Button variant="ghost" title="Delete" onClick={() => del.mutate(i.id, { onError: errorToast(pushToast) })}><Trash2 size={14} /></Button>
            </div>
          )}
        </div>
      ))}
      <AgendaModal key={editing === null ? 'closed' : editing === 'new' ? `new-${scope}` : editing.id} editing={editing} scope={scope} onClose={() => setEditing(null)} save={save} />
      <AgendaImportModal key={importing ? `import-${scope}` : 'import-closed'} open={importing} scope={scope} onClose={() => setImporting(false)} />
    </Card>
  );
}

const IMPORT_HINT = `# one item per line: HH:MM[-HH:MM] Title [| details]
09:00 Doors open | Teams check in at registration
10:30-11:00 Opening ceremony
12:00-13:00 Lunch`;

/** Paste a run of show; check it (dry run) before adding. All-or-nothing:
 * any bad line is reported by number and nothing is written. */
function AgendaImportModal({ open, scope, onClose }: { open: boolean; scope: 'day' | 'finals'; onClose: () => void }) {
  const run = useImportAgenda();
  const pushToast = useUiStore((s) => s.pushToast);
  const [text, setText] = useState('');
  const [replace, setReplace] = useState(false);
  const [problems, setProblems] = useState<{ line: number; error: string }[]>([]);
  const [preview, setPreview] = useState<number | null>(null);

  function submit(dryRun: boolean) {
    run.mutate(
      { scope, text, replace, dry_run: dryRun },
      {
        onSuccess: (result) => {
          setProblems(result.errors);
          if (result.errors.length) return setPreview(null);
          if (dryRun) return setPreview(result.items.length);
          pushToast(`Imported ${result.created} item${result.created === 1 ? '' : 's'}`, 'success');
          onClose();
        },
        onError: errorToast(pushToast),
      },
    );
  }

  return (
    <Modal open={open} onClose={onClose} title={`Import ${scope === 'day' ? 'day' : 'finals'} agenda`}>
      <div className="space-y-3">
        <textarea
          className="vmd-code-block h-48 w-full rounded-lg border border-vmd-border bg-vmd-elevated p-2 font-mono text-xs"
          placeholder={IMPORT_HINT}
          aria-label="Agenda text"
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setPreview(null);
            setProblems([]);
          }}
          spellCheck={false}
        />
        <label className="flex items-center gap-2 text-sm text-vmd-textMuted">
          <Toggle checked={replace} onChange={setReplace} /> Replace the existing {scope} agenda
        </label>
        {problems.length > 0 && (
          <ul className="text-xs text-vmd-danger" data-testid="import-errors">
            {problems.map((p) => (
              <li key={`${p.line}-${p.error}`}>Line {p.line}: {p.error}</li>
            ))}
          </ul>
        )}
        {preview !== null && <p className="text-xs text-vmd-success" data-testid="import-preview">{preview} item{preview === 1 ? '' : 's'} look good.</p>}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="secondary" disabled={!text.trim() || run.isPending} onClick={() => submit(true)}>Check</Button>
          <Button variant="primary" disabled={!text.trim() || run.isPending} onClick={() => submit(false)}>Import</Button>
        </div>
      </div>
    </Modal>
  );
}

function AgendaModal({
  editing,
  scope,
  onClose,
  save,
}: {
  editing: AgendaItem | 'new' | null;
  scope: 'day' | 'finals';
  onClose: () => void;
  save: ReturnType<typeof useSaveAgendaItem>;
}) {
  const existing = editing && editing !== 'new' ? editing : null;
  const pushToast = useUiStore((s) => s.pushToast);
  const [start, setStart] = useState(existing?.start_time ?? '');
  const [end, setEnd] = useState(existing?.end_time ?? '');
  const [title, setTitle] = useState(existing?.title ?? '');
  const [details, setDetails] = useState(existing?.details ?? '');
  const [announce, setAnnounce] = useState(existing?.announce_lead_minutes == null ? '' : String(existing.announce_lead_minutes));
  const announceNum = announce.trim() === '' ? null : Number(announce);
  const announceOk = announceNum === null || (Number.isInteger(announceNum) && announceNum >= 0 && announceNum <= 240);
  const valid = announceOk && TIME_PATTERN.test(start) && (!end || TIME_PATTERN.test(end)) && !!title.trim();
  return (
    <Modal open={editing !== null} onClose={onClose} title={existing ? 'Edit agenda item' : `New ${scope} agenda item`}>
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <div><Label>Start (HH:MM)</Label><Input placeholder="09:15" value={start} onChange={(e) => setStart(e.target.value)} /></div>
          <div><Label>End (optional)</Label><Input placeholder="09:45" value={end} onChange={(e) => setEnd(e.target.value)} /></div>
        </div>
        <div><Label>Title</Label><Input value={title} onChange={(e) => setTitle(e.target.value)} /></div>
        <div><Label>Details (optional)</Label><Input value={details} onChange={(e) => setDetails(e.target.value)} /></div>
        <div>
          <Label>Announce to rooms N minutes before (blank = default, 0 = never)</Label>
          <Input type="number" min={0} max={240} value={announce} onChange={(e) => setAnnounce(e.target.value)} />
        </div>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            disabled={!valid || save.isPending}
            onClick={() =>
              save.mutate(
                { id: existing?.id, body: { scope: existing?.scope ?? scope, start_time: start, end_time: end || null, title: title.trim(), details: details.trim() || null, announce_lead_minutes: announceNum } },
                { onSuccess: onClose, onError: errorToast(pushToast) },
              )
            }
          >
            Save
          </Button>
        </div>
      </div>
    </Modal>
  );
}
