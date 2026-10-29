import { useState } from 'react';
import { Check, ExternalLink, Pencil, Plus, Send, Trash2, Zap } from 'lucide-react';
import {
  useAckMessage,
  useAgenda,
  useDeleteAgendaItem,
  useDeletePreset,
  useDeleteStation,
  useInbox,
  usePresets,
  usePushScreen,
  useSaveAgendaItem,
  useSavePreset,
  useSaveStation,
  useSendMessage,
  useStations,
  type Audience,
} from '../api/comms';
import { usePermission } from '../hooks/usePermission';
import { useUiStore } from '../stores/ui';
import { Button } from '../components/ui/Button';
import { Card, PageHeader } from '../components/ui/Card';
import { CopyButton } from '../components/ui/CopyButton';
import { Input, Label, Select } from '../components/ui/Input';
import { Modal } from '../components/ui/Modal';
import { Toggle } from '../components/ui/Toggle';
import type { AgendaItem, CommsPreset, CommsScreen, Station, StationRole } from '../types/api';

const ROLE_LABEL: Record<StationRole, string> = { emcee: 'Emcees', floor_manager: 'Floor managers', camera: 'Camera companions' };
const SCREEN_LABEL: Record<CommsScreen, string> = {
  messages: 'Messages',
  countdown: 'Next match countdown (emcees)',
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

function StationsCard({ stations, canManage, onEdit }: { stations: Station[]; canManage: boolean; onEdit: (s: Station | 'new') => void }) {
  const del = useDeleteStation();
  const pushToast = useUiStore((s) => s.pushToast);
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
      {stations.map((s) => {
        const url = `${window.location.origin}/station/${s.slug}`;
        return (
          <div key={s.slug} className="flex flex-wrap items-center justify-between gap-2 border-b border-vmd-border py-2 last:border-0">
            <div>
              <p className="text-sm font-medium text-vmd-textStrong">{s.name}</p>
              <p className="text-xs text-vmd-textSubtle">{ROLE_LABEL[s.role]} · /station/{s.slug}</p>
            </div>
            <div className="flex gap-1">
              <CopyButton value={url} />
              <a href={`/station/${s.slug}`} target="_blank" rel="noreferrer">
                <Button variant="ghost" type="button" title="Open"><ExternalLink size={14} /></Button>
              </a>
              {canManage && (
                <>
                  <Button variant="ghost" onClick={() => onEdit(s)} title="Edit"><Pencil size={14} /></Button>
                  <Button
                    variant="ghost"
                    title="Delete"
                    onClick={() => window.confirm(`Delete station "${s.name}"?`) && del.mutate(s.slug, { onError: errorToast(pushToast) })}
                  >
                    <Trash2 size={14} />
                  </Button>
                </>
              )}
            </div>
          </div>
        );
      })}
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
  const pushToast = useUiStore((s) => s.pushToast);
  const [scope, setScope] = useState<'day' | 'finals'>('day');
  const [editing, setEditing] = useState<AgendaItem | 'new' | null>(null);
  const shown = items.filter((i) => i.scope === scope);
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
          <Button variant="secondary" onClick={() => setEditing('new')}>
            <Plus size={14} /> Add item
          </Button>
        )}
      </div>
      {shown.length === 0 && <p className="text-sm text-vmd-textSubtle">No items.</p>}
      {shown.map((i) => (
        <div key={i.id} className="flex items-center justify-between gap-2 border-b border-vmd-border py-2 last:border-0">
          <div>
            <p className="text-sm text-vmd-textStrong"><span className="mr-3 tabular-nums text-vmd-textMuted">{i.start_time}{i.end_time ? `–${i.end_time}` : ''}</span>{i.title}</p>
            {i.details && <p className="text-xs text-vmd-textSubtle">{i.details}</p>}
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
    </Card>
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
  const valid = TIME_PATTERN.test(start) && (!end || TIME_PATTERN.test(end)) && !!title.trim();
  return (
    <Modal open={editing !== null} onClose={onClose} title={existing ? 'Edit agenda item' : `New ${scope} agenda item`}>
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <div><Label>Start (HH:MM)</Label><Input placeholder="09:15" value={start} onChange={(e) => setStart(e.target.value)} /></div>
          <div><Label>End (optional)</Label><Input placeholder="09:45" value={end} onChange={(e) => setEnd(e.target.value)} /></div>
        </div>
        <div><Label>Title</Label><Input value={title} onChange={(e) => setTitle(e.target.value)} /></div>
        <div><Label>Details (optional)</Label><Input value={details} onChange={(e) => setDetails(e.target.value)} /></div>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            disabled={!valid || save.isPending}
            onClick={() =>
              save.mutate(
                { id: existing?.id, body: { scope: existing?.scope ?? scope, start_time: start, end_time: end || null, title: title.trim(), details: details.trim() || null, position: existing?.position ?? 0 } },
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
