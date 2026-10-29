import { useEffect, useRef, useState, type ComponentProps } from 'react';
import { Plus, Trash2, Upload } from 'lucide-react';
import { usePublicOverview, useUploadSiteImage } from '../../api/site';
import {
  DEFAULT_PANEL_SECONDS,
  DEFAULT_PROGRAMME_SECONDS,
  parseTeamList,
  percentPosition,
  siteProblems,
  type SiteBus,
  type SiteConfig,
  type SiteRoom,
  type SiteSponsor,
} from '../../lib/siteConfig';
import { useUiStore } from '../../stores/ui';
import { Button } from '../ui/Button';
import { Input, Label, Textarea } from '../ui/Input';

type Patch<T> = (next: T) => void;

/** A comma-separated team list. It keeps its own text so separators typed
 * mid-edit aren't swallowed by re-rendering from the parsed list. */
function TeamListInput({ value, onChange, ...rest }: { value: string[]; onChange: Patch<string[]> } & Omit<ComponentProps<typeof Input>, 'value' | 'onChange'>) {
  const [text, setText] = useState(value.join(', '));
  useEffect(() => {
    if (parseTeamList(text).join('|') !== value.join('|')) setText(value.join(', '));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  return (
    <Input
      {...rest}
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        onChange(parseTeamList(e.target.value));
      }}
    />
  );
}

/** Form for the `site` setting (the public website). Edits are reported
 * through `onChange` as a whole new config; keys it doesn't know are kept. */
export function SiteConfigEditor({
  value,
  onChange,
  disabled,
}: {
  value: SiteConfig;
  onChange: Patch<SiteConfig>;
  disabled?: boolean;
}) {
  const set = (patch: Partial<SiteConfig>) => onChange({ ...value, ...patch });
  const nc = value.next_competition ?? {};
  const setNc = (patch: Partial<NonNullable<SiteConfig['next_competition']>>) => set({ next_competition: { ...nc, ...patch } });
  const problems = siteProblems(value);
  const lb = value.leaderboard ?? {};
  const bus = value.bus_tracker ?? {};
  const setBus = (patch: Partial<NonNullable<SiteConfig['bus_tracker']>>) => set({ bus_tracker: { ...bus, ...patch } });

  return (
    <div className="space-y-5" data-testid="site-config">
      <section className="grid gap-3 md:grid-cols-2">
        <div>
          <Label>Site title</Label>
          <Input value={value.title ?? ''} onChange={(e) => set({ title: e.target.value })} disabled={disabled} aria-label="Site title" />
        </div>
        <div>
          <Label>“Presented by” line</Label>
          <Input value={value.presented_by ?? ''} onChange={(e) => set({ presented_by: e.target.value })} disabled={disabled} aria-label="Presented by" />
        </div>
      </section>

      <section>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-vmd-textMuted">Next competition</h3>
        <div className="grid gap-3 md:grid-cols-3">
          <div>
            <Label>Name</Label>
            <Input value={nc.name ?? ''} onChange={(e) => setNc({ name: e.target.value })} disabled={disabled} aria-label="Next competition name" />
          </div>
          <div>
            <Label>Date (YYYY-MM-DD)</Label>
            <Input value={nc.date ?? ''} placeholder="2027-01-10" onChange={(e) => setNc({ date: e.target.value })} disabled={disabled} aria-label="Next competition date" />
          </div>
          <div>
            <Label>Location</Label>
            <Input value={nc.location ?? ''} onChange={(e) => setNc({ location: e.target.value })} disabled={disabled} aria-label="Next competition location" />
          </div>
          <div>
            <Label>Event page link</Label>
            <Input value={nc.url ?? ''} onChange={(e) => setNc({ url: e.target.value })} disabled={disabled} aria-label="Event page link" />
          </div>
          <div>
            <Label>Registration link</Label>
            <Input value={nc.registration_url ?? ''} onChange={(e) => setNc({ registration_url: e.target.value })} disabled={disabled} aria-label="Registration link" />
          </div>
          <div>
            <Label>Teams attending (comma separated)</Label>
            <TeamListInput value={nc.teams ?? []} onChange={(teams) => setNc({ teams })} disabled={disabled} aria-label="Teams attending" />
          </div>
          <div className="md:col-span-3">
            <Label>Details</Label>
            <Textarea rows={2} className="w-full text-sm" value={nc.description ?? ''} onChange={(e) => setNc({ description: e.target.value })} disabled={disabled} aria-label="Next competition details" />
          </div>
        </div>
      </section>

      <SponsorsEditor sponsors={value.sponsors ?? []} onChange={(sponsors) => set({ sponsors })} disabled={disabled} />
      <MapEditor map={value.map ?? {}} onChange={(map) => set({ map })} disabled={disabled} />

      <section>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-vmd-textMuted">Leaderboards (/site/leaderboard)</h3>
        <div className="grid gap-3 md:grid-cols-2">
          <div>
            <Label>Seconds per panel (overall → tournament → skills → awards)</Label>
            <Input
              type="number"
              min={5}
              max={3600}
              value={lb.panel_seconds ?? DEFAULT_PANEL_SECONDS}
              onChange={(e) => set({ leaderboard: { ...lb, panel_seconds: Number(e.target.value) } })}
              disabled={disabled}
              aria-label="Seconds per panel"
            />
          </div>
          <div>
            <Label>Seconds before swapping IQ ↔ VRC</Label>
            <Input
              type="number"
              min={5}
              max={3600}
              value={lb.programme_seconds ?? DEFAULT_PROGRAMME_SECONDS}
              onChange={(e) => set({ leaderboard: { ...lb, programme_seconds: Number(e.target.value) } })}
              disabled={disabled}
              aria-label="Seconds per programme"
            />
          </div>
        </div>
      </section>

      <section>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-vmd-textMuted">Match recordings</h3>
        <Label>Recording URL template (used for scored matches without their own link)</Label>
        <Input
          placeholder="https://example.com/recordings/{division}/{label}.mp4"
          value={value.recording_url_template ?? ''}
          onChange={(e) => set({ recording_url_template: e.target.value })}
          disabled={disabled}
          aria-label="Recording URL template"
        />
        <p className="mt-1 text-xs text-vmd-textSubtle">
          Placeholders: <code>{'{division}'}</code> <code>{'{label}'}</code> <code>{'{round}'}</code> <code>{'{instance}'}</code> <code>{'{match}'}</code>. Individual links are managed below.
        </p>
      </section>

      <section>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-vmd-textMuted">Bus tracker</h3>
        <div className="grid gap-3 md:grid-cols-2">
          <div>
            <Label>Title</Label>
            <Input value={bus.title ?? ''} placeholder="Bus tracker" onChange={(e) => setBus({ title: e.target.value })} disabled={disabled} aria-label="Bus tracker title" />
          </div>
          <div>
            <Label>Embedded tracker page (https)</Label>
            <Input value={bus.embed_url ?? ''} onChange={(e) => setBus({ embed_url: e.target.value })} disabled={disabled} aria-label="Bus tracker embed URL" />
          </div>
          <div>
            <Label>Link to open the tracker</Label>
            <Input value={bus.link_url ?? ''} onChange={(e) => setBus({ link_url: e.target.value })} disabled={disabled} aria-label="Bus tracker link" />
          </div>
          <div>
            <Label>Note</Label>
            <Input value={bus.note ?? ''} onChange={(e) => setBus({ note: e.target.value })} disabled={disabled} aria-label="Bus tracker note" />
          </div>
        </div>
        <BusList buses={bus.buses ?? []} onChange={(buses) => setBus({ buses })} disabled={disabled} />
      </section>

      {problems.length > 0 && (
        <ul className="text-xs text-vmd-danger" data-testid="site-problems">
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ImageUpload({ label, onUploaded, disabled }: { label: string; onUploaded: (key: string, url: string | null) => void; disabled?: boolean }) {
  const upload = useUploadSiteImage();
  const pushToast = useUiStore((s) => s.pushToast);
  const input = useRef<HTMLInputElement>(null);
  return (
    <>
      <input
        ref={input}
        type="file"
        accept="image/png,image/jpeg,image/gif,image/webp"
        className="hidden"
        aria-label={label}
        onChange={(e) => {
          const file = e.target.files?.[0];
          e.target.value = '';
          if (!file) return;
          upload.mutate(file, {
            onSuccess: (res) => onUploaded(res.key, res.url),
            onError: (err) => pushToast(err instanceof Error ? err.message : 'Upload failed', 'error'),
          });
        }}
      />
      <Button variant="secondary" type="button" disabled={disabled || upload.isPending} onClick={() => input.current?.click()}>
        <Upload size={14} /> {upload.isPending ? 'Uploading…' : label}
      </Button>
    </>
  );
}

function SponsorsEditor({ sponsors, onChange, disabled }: { sponsors: SiteSponsor[]; onChange: Patch<SiteSponsor[]>; disabled?: boolean }) {
  const [previews, setPreviews] = useState<Record<string, string>>({});
  const update = (i: number, patch: Partial<SiteSponsor>) => onChange(sponsors.map((s, j) => (j === i ? { ...s, ...patch } : s)));
  return (
    <section>
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-vmd-textMuted">Sponsors</h3>
        <Button variant="secondary" type="button" disabled={disabled} onClick={() => onChange([...sponsors, { name: '' }])}>
          <Plus size={14} /> Add sponsor
        </Button>
      </div>
      {sponsors.length === 0 && <p className="text-sm text-vmd-textSubtle">No sponsors.</p>}
      {sponsors.map((s, i) => (
        <div key={i} className="mb-2 grid items-end gap-2 md:grid-cols-[1fr_1fr_auto_auto]" data-testid="sponsor-row">
          <div>
            <Label>Name</Label>
            <Input value={s.name} onChange={(e) => update(i, { name: e.target.value })} disabled={disabled} aria-label={`Sponsor ${i + 1} name`} />
          </div>
          <div>
            <Label>Link</Label>
            <Input value={s.url ?? ''} onChange={(e) => update(i, { url: e.target.value })} disabled={disabled} aria-label={`Sponsor ${i + 1} link`} />
          </div>
          <div className="flex items-center gap-2">
            {s.logo_key && previews[s.logo_key] ? (
              <img src={previews[s.logo_key]} alt="" className="h-9 max-w-[5rem] rounded object-contain" />
            ) : (
              s.logo_key && <span className="text-xs text-vmd-textSubtle">logo set</span>
            )}
            <ImageUpload
              label={`Upload logo ${i + 1}`}
              disabled={disabled}
              onUploaded={(key, url) => {
                if (url) setPreviews((p) => ({ ...p, [key]: url }));
                update(i, { logo_key: key });
              }}
            />
          </div>
          <Button variant="ghost" type="button" disabled={disabled} aria-label={`Remove sponsor ${i + 1}`} onClick={() => onChange(sponsors.filter((_, j) => j !== i))}>
            <Trash2 size={14} />
          </Button>
        </div>
      ))}
    </section>
  );
}

function MapEditor({ map, onChange, disabled }: { map: NonNullable<SiteConfig['map']>; onChange: Patch<NonNullable<SiteConfig['map']>>; disabled?: boolean }) {
  const { data: overview } = usePublicOverview();
  const [preview, setPreview] = useState<{ key: string; url: string } | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const rooms = map.rooms ?? [];
  const imageUrl = preview && preview.key === map.image_key ? preview.url : (overview?.map?.image_url ?? map.image_url ?? null);
  const updateRoom = (i: number, patch: Partial<SiteRoom>) => onChange({ ...map, rooms: rooms.map((r, j) => (j === i ? { ...r, ...patch } : r)) });

  return (
    <section>
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-vmd-textMuted">Event map</h3>
        <div className="flex gap-2">
          <ImageUpload
            label="Upload map image"
            disabled={disabled}
            onUploaded={(key, url) => {
              if (url) setPreview({ key, url });
              onChange({ ...map, image_key: key, image_url: undefined });
            }}
          />
          <Button
            variant="secondary"
            type="button"
            disabled={disabled}
            onClick={() => {
              onChange({ ...map, rooms: [...rooms, { name: '', x: 50, y: 50 }] });
              setSelected(rooms.length);
            }}
          >
            <Plus size={14} /> Add room pin
          </Button>
        </div>
      </div>
      {imageUrl ? (
        <>
          <p className="mb-1 text-xs text-vmd-textSubtle">
            {selected !== null ? `Click the map to place “${rooms[selected]?.name || 'the new pin'}”.` : 'Select a room below, then click the map to place its pin.'}
          </p>
          <div
            className="relative max-h-96 cursor-crosshair overflow-auto rounded-lg border border-vmd-border bg-black"
            data-testid="map-canvas"
            onClick={(e) => {
              if (disabled || selected === null) return;
              const rect = e.currentTarget.querySelector('img')?.getBoundingClientRect();
              if (rect) updateRoom(selected, percentPosition(e, rect));
            }}
          >
            <div className="relative">
              <img src={imageUrl} alt="Event map" className="block w-full" draggable={false} />
              {rooms.map((r, i) => (
                <span
                  key={i}
                  className={`pointer-events-none absolute -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white px-2 text-xs text-white ${selected === i ? 'bg-vmd-warning' : 'bg-vmd-accent'}`}
                  style={{ left: `${r.x}%`, top: `${r.y}%` }}
                >
                  {r.name || '?'}
                </span>
              ))}
            </div>
          </div>
        </>
      ) : (
        <p className="text-sm text-vmd-textSubtle">Upload a map image to place room pins on it.</p>
      )}
      {rooms.map((r, i) => (
        <div
          key={i}
          data-testid="room-row"
          className={`mt-2 grid items-end gap-2 rounded-lg border p-2 md:grid-cols-[1fr_1fr_5rem_5rem_auto] ${selected === i ? 'border-vmd-accent' : 'border-vmd-border'}`}
        >
          <div>
            <Label>Room</Label>
            <Input value={r.name} onChange={(e) => updateRoom(i, { name: e.target.value })} disabled={disabled} aria-label={`Room ${i + 1} name`} onFocus={() => setSelected(i)} />
          </div>
          <div>
            <Label>Teams here (optional)</Label>
            <TeamListInput value={r.teams ?? []} onChange={(teams) => updateRoom(i, { teams })} disabled={disabled} aria-label={`Room ${i + 1} teams`} onFocus={() => setSelected(i)} />
          </div>
          <div>
            <Label>X %</Label>
            <Input type="number" min={0} max={100} value={r.x} onChange={(e) => updateRoom(i, { x: Number(e.target.value) })} disabled={disabled} aria-label={`Room ${i + 1} x`} />
          </div>
          <div>
            <Label>Y %</Label>
            <Input type="number" min={0} max={100} value={r.y} onChange={(e) => updateRoom(i, { y: Number(e.target.value) })} disabled={disabled} aria-label={`Room ${i + 1} y`} />
          </div>
          <Button
            variant="ghost"
            type="button"
            disabled={disabled}
            aria-label={`Remove room ${i + 1}`}
            onClick={() => {
              onChange({ ...map, rooms: rooms.filter((_, j) => j !== i) });
              setSelected(null);
            }}
          >
            <Trash2 size={14} />
          </Button>
        </div>
      ))}
    </section>
  );
}

function BusList({ buses, onChange, disabled }: { buses: SiteBus[]; onChange: Patch<SiteBus[]>; disabled?: boolean }) {
  const update = (i: number, patch: Partial<SiteBus>) => onChange(buses.map((b, j) => (j === i ? { ...b, ...patch } : b)));
  return (
    <div className="mt-3">
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs text-vmd-textMuted">Status board (optional)</span>
        <Button variant="secondary" type="button" disabled={disabled} onClick={() => onChange([...buses, { name: '' }])}>
          <Plus size={14} /> Add bus
        </Button>
      </div>
      {buses.map((b, i) => (
        <div key={i} className="mb-2 grid items-end gap-2 md:grid-cols-[1fr_2fr_7rem_auto]">
          <Input value={b.name} placeholder="Bus 1" onChange={(e) => update(i, { name: e.target.value })} disabled={disabled} aria-label={`Bus ${i + 1} name`} />
          <Input value={b.status ?? ''} placeholder="Status, e.g. At school" onChange={(e) => update(i, { status: e.target.value })} disabled={disabled} aria-label={`Bus ${i + 1} status`} />
          <Input value={b.eta ?? ''} placeholder="ETA" onChange={(e) => update(i, { eta: e.target.value })} disabled={disabled} aria-label={`Bus ${i + 1} ETA`} />
          <Button variant="ghost" type="button" disabled={disabled} aria-label={`Remove bus ${i + 1}`} onClick={() => onChange(buses.filter((_, j) => j !== i))}>
            <Trash2 size={14} />
          </Button>
        </div>
      ))}
    </div>
  );
}
