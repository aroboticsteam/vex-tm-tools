import { useState } from 'react';
import { ExternalLink, Pencil, Plus, QrCode, Send, Trash2 } from 'lucide-react';
import { useBroadcast, useNotificationStats, useTestPush } from '../api/notifications';
import { useCreateRoom, useDeleteRoom, useRooms, useUpdateRoom } from '../api/rooms';
import { usePermission } from '../hooks/usePermission';
import { useUiStore } from '../stores/ui';
import { Button } from '../components/ui/Button';
import { Card, PageHeader } from '../components/ui/Card';
import { CopyButton } from '../components/ui/CopyButton';
import { Input, Label, Textarea } from '../components/ui/Input';
import { Modal } from '../components/ui/Modal';
import { Toggle } from '../components/ui/Toggle';
import type { Room } from '../types/api';

const SLUG_PATTERN = /^[a-z0-9][a-z0-9-]{0,49}$/;

function parseTeams(text: string): string[] {
  const seen: string[] = [];
  for (const raw of text.split(/[\s,;]+/)) {
    const team = raw.trim().toUpperCase();
    if (team && !seen.includes(team)) seen.push(team);
  }
  return seen;
}

/** Rooms: named groups of teams. Each has a public display page (`/room/<slug>`,
 * with an optional livestream embed and live match alerts) and a QR code that
 * lets people in the room subscribe to alerts for just that room's teams. */
export function RoomsPage() {
  const { data: rooms, isLoading } = useRooms();
  const canManage = usePermission('rooms:manage');
  const canBroadcast = usePermission('notifications:broadcast');
  const canSeeStats = usePermission('notifications:manage');
  const { data: stats } = useNotificationStats(canSeeStats);
  const deleteRoom = useDeleteRoom();
  const pushToast = useUiStore((s) => s.pushToast);

  const [editing, setEditing] = useState<Room | 'new' | null>(null);
  const [qrRoom, setQrRoom] = useState<Room | null>(null);

  function remove(room: Room) {
    if (!window.confirm(`Delete room "${room.name}"? Its phone subscriptions are kept but lose their room.`)) return;
    deleteRoom.mutate(room.slug, {
      onSuccess: () => pushToast('Room deleted', 'success'),
      onError: (err) => pushToast(err instanceof Error ? err.message : 'Delete failed', 'error'),
    });
  }

  return (
    <div>
      <PageHeader
        title="Rooms"
        subtitle="Team allocations with a display page (livestream + match alerts) and a QR code for phone notifications."
        action={
          canManage && (
            <Button variant="primary" onClick={() => setEditing('new')}>
              <Plus size={14} /> New room
            </Button>
          )
        }
      />

      {stats && !stats.push_configured && (
        <p className="mb-4 rounded-lg border border-vmd-border bg-vmd-surfaceSubtle px-3 py-2 text-sm text-vmd-textMuted">
          Phone push notifications are not configured on this server (set <code>VAPID_PRIVATE_KEY</code> /{' '}
          <code>VAPID_PUBLIC_KEY</code>; see <code>tools/generate_vapid_keys.py</code>). Room displays still receive alerts.
        </p>
      )}

      {isLoading && <p className="text-sm text-vmd-textSubtle">Loading…</p>}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {(rooms ?? []).map((room) => (
          <Card key={room.slug}>
            <div className="flex items-start justify-between gap-2">
              <div>
                <p className="font-medium text-vmd-textStrong">{room.name}</p>
                <p className="text-xs text-vmd-textSubtle">/room/{room.slug}</p>
              </div>
              {stats && <span className="vmd-chip">{stats.subscriptions_by_room[room.slug] ?? 0} phones</span>}
            </div>
            <p className="mt-2 text-sm text-vmd-textMuted">
              {room.teams.length} team{room.teams.length === 1 ? '' : 's'}
              {room.stream_url ? ' · livestream' : ''}
            </p>
            <p className="mt-1 line-clamp-2 text-xs text-vmd-textSubtle">{room.teams.join(', ') || 'No teams yet'}</p>
            <div className="mt-3 flex flex-wrap gap-1.5">
              <a href={`/room/${room.slug}`} target="_blank" rel="noreferrer">
                <Button variant="secondary" type="button">
                  <ExternalLink size={14} /> Display
                </Button>
              </a>
              <Button variant="secondary" onClick={() => setQrRoom(room)}>
                <QrCode size={14} /> QR
              </Button>
              {canManage && (
                <>
                  <Button variant="ghost" onClick={() => setEditing(room)} title="Edit">
                    <Pencil size={14} />
                  </Button>
                  <Button variant="ghost" onClick={() => remove(room)} title="Delete">
                    <Trash2 size={14} />
                  </Button>
                </>
              )}
            </div>
          </Card>
        ))}
      </div>
      {!isLoading && (rooms ?? []).length === 0 && (
        <p className="text-sm text-vmd-textSubtle">No rooms yet. Create one and assign the teams that are in it.</p>
      )}

      {canBroadcast && (rooms ?? []).length > 0 && <BroadcastCard rooms={rooms ?? []} />}
      {canSeeStats && stats?.push_configured && <StaffPushCard staffCount={stats.staff_subscriptions} />}

      <RoomFormModal key={editing === null ? 'closed' : editing === 'new' ? 'new' : editing.slug} editing={editing} onClose={() => setEditing(null)} />
      <QrModal room={qrRoom} onClose={() => setQrRoom(null)} />
    </div>
  );
}

function RoomFormModal({ editing, onClose }: { editing: Room | 'new' | null; onClose: () => void }) {
  const existing = editing && editing !== 'new' ? editing : null;
  const createRoom = useCreateRoom();
  const updateRoom = useUpdateRoom();
  const pushToast = useUiStore((s) => s.pushToast);

  const [slug, setSlug] = useState(existing?.slug ?? '');
  const [name, setName] = useState(existing?.name ?? '');
  const [streamUrl, setStreamUrl] = useState(existing?.stream_url ?? '');
  const [teamsText, setTeamsText] = useState(existing?.teams.join(', ') ?? '');

  const slugOk = existing !== null || SLUG_PATTERN.test(slug);
  const busy = createRoom.isPending || updateRoom.isPending;

  function submit() {
    const body = { name: name.trim(), stream_url: streamUrl.trim() || null, teams: parseTeams(teamsText) };
    const callbacks = {
      onSuccess: () => {
        pushToast(existing ? 'Room updated' : 'Room created', 'success');
        onClose();
      },
      onError: (err: unknown) => pushToast(err instanceof Error ? err.message : 'Save failed', 'error'),
    };
    if (existing) updateRoom.mutate({ slug: existing.slug, body }, callbacks);
    else createRoom.mutate({ slug: slug.trim(), ...body }, callbacks);
  }

  return (
    <Modal open={editing !== null} onClose={onClose} title={existing ? `Edit ${existing.name}` : 'New room'}>
      <div className="space-y-3">
        {!existing && (
          <div>
            <Label>URL name (lowercase letters, numbers, dashes)</Label>
            <Input placeholder="pit-room-1" value={slug} onChange={(e) => setSlug(e.target.value.toLowerCase())} />
            {slug && !slugOk && <p className="mt-1 text-xs text-vmd-danger">Use a-z, 0-9 and dashes only.</p>}
          </div>
        )}
        <div>
          <Label>Display name</Label>
          <Input value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div>
          <Label>Livestream URL (optional — YouTube links are converted to embeds)</Label>
          <Input placeholder="https://www.youtube.com/watch?v=…" value={streamUrl} onChange={(e) => setStreamUrl(e.target.value)} />
        </div>
        <div>
          <Label>Teams in this room (commas, spaces or new lines)</Label>
          <Textarea rows={5} value={teamsText} onChange={(e) => setTeamsText(e.target.value)} />
          <p className="mt-1 text-xs text-vmd-textSubtle">{parseTeams(teamsText).length} teams</p>
        </div>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" onClick={submit} disabled={busy || !name.trim() || !slugOk || (!existing && !slug)}>
            Save
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function QrModal({ room, onClose }: { room: Room | null; onClose: () => void }) {
  const notifyUrl = room ? `${window.location.origin}/notify?room=${encodeURIComponent(room.slug)}` : '';
  return (
    <Modal open={room !== null} onClose={onClose} title={room ? `${room.name} — phone alerts QR` : ''}>
      {room && (
        <div className="space-y-3 text-center">
          <img
            src={`/room/${encodeURIComponent(room.slug)}/qr.svg`}
            alt={`QR code for ${room.name}`}
            className="mx-auto w-64 rounded-lg bg-white p-2"
          />
          <p className="break-all text-xs text-vmd-textSubtle">{notifyUrl}</p>
          <div className="flex justify-center gap-2">
            <CopyButton value={notifyUrl} />
            <a href={`/room/${room.slug}/qr.svg`} download={`${room.slug}-qr.svg`}>
              <Button variant="secondary" type="button">
                Download SVG
              </Button>
            </a>
          </div>
          <p className="text-xs text-vmd-textSubtle">
            The room display page shows this QR automatically. Set <code>PUBLIC_BASE_URL</code> on the server if the
            address above isn&apos;t the one phones should use.
          </p>
        </div>
      )}
    </Modal>
  );
}

function BroadcastCard({ rooms }: { rooms: Room[] }) {
  const broadcast = useBroadcast();
  const pushToast = useUiStore((s) => s.pushToast);
  const [title, setTitle] = useState('');
  const [message, setMessage] = useState('');
  const [selected, setSelected] = useState<string[]>([]); // empty = all rooms
  const [push, setPush] = useState(true);
  const [modal, setModal] = useState(true);

  function toggleRoom(slug: string) {
    setSelected((cur) => (cur.includes(slug) ? cur.filter((s) => s !== slug) : [...cur, slug]));
  }

  function send() {
    broadcast.mutate(
      { title: title.trim(), message: message.trim(), rooms: selected.length ? selected : null, push, modal },
      {
        onSuccess: (res) => {
          pushToast(`Sent to ${res.rooms} room display${res.rooms === 1 ? '' : 's'}, ${res.pushed} phone${res.pushed === 1 ? '' : 's'}`, 'success');
          setTitle('');
          setMessage('');
        },
        onError: (err) => pushToast(err instanceof Error ? err.message : 'Broadcast failed', 'error'),
      },
    );
  }

  return (
    <Card className="mt-6">
      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">Broadcast</h2>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="space-y-2">
          <div>
            <Label>Title</Label>
            <Input maxLength={100} value={title} onChange={(e) => setTitle(e.target.value)} />
          </div>
          <div>
            <Label>Message</Label>
            <Textarea rows={3} maxLength={500} className="font-sans" value={message} onChange={(e) => setMessage(e.target.value)} />
          </div>
          <div className="flex items-center justify-between">
            <Label>Also push to phones</Label>
            <Toggle checked={push} onChange={setPush} />
          </div>
          <div className="flex items-center justify-between">
            <Label>Full-screen popup on room displays</Label>
            <Toggle checked={modal} onChange={setModal} />
          </div>
        </div>
        <div>
          <Label>Rooms ({selected.length === 0 ? 'all rooms' : `${selected.length} selected`})</Label>
          <div className="max-h-40 space-y-1 overflow-auto rounded-lg border border-vmd-border p-2">
            {rooms.map((room) => (
              <label key={room.slug} className="flex items-center gap-2 text-sm text-vmd-text">
                <input type="checkbox" checked={selected.includes(room.slug)} onChange={() => toggleRoom(room.slug)} />
                {room.name}
              </label>
            ))}
          </div>
          <p className="mt-1 text-xs text-vmd-textSubtle">Leave all unchecked to send to every room.</p>
          <Button variant="primary" className="mt-3" onClick={send} disabled={broadcast.isPending || !title.trim() || !message.trim()}>
            <Send size={14} /> Send broadcast
          </Button>
        </div>
      </div>
    </Card>
  );
}

function StaffPushCard({ staffCount }: { staffCount: number }) {
  const testPush = useTestPush();
  const pushToast = useUiStore((s) => s.pushToast);
  return (
    <Card className="mt-6">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">Staff push alerts</h2>
          <p className="mt-1 text-sm text-vmd-textMuted">
            {staffCount} staff device{staffCount === 1 ? '' : 's'} subscribed. Staff turn alerts on with the bell in the top bar.
          </p>
        </div>
        <Button
          variant="secondary"
          disabled={testPush.isPending || staffCount === 0}
          onClick={() =>
            testPush.mutate(undefined, {
              onSuccess: (res) => pushToast(`Test sent to ${res.pushed} device${res.pushed === 1 ? '' : 's'}`, 'success'),
              onError: (err) => pushToast(err instanceof Error ? err.message : 'Test failed', 'error'),
            })
          }
        >
          Send test
        </Button>
      </div>
    </Card>
  );
}
