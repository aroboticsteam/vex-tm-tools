import { useEffect, useState } from 'react';
import { Plus, Trash2, RefreshCw } from 'lucide-react';
import { useApiKeys, useCreateApiKey, useRevokeApiKey, useSettings, useUpdateSettings } from '../api/settings';
import { usePermission } from '../hooks/usePermission';
import { useUiStore } from '../stores/ui';
import { ALL_PERMISSIONS } from '../stores/auth';
import { PageHeader, Card } from '../components/ui/Card';
import { Button } from '../components/ui/Button';
import { Input, Label } from '../components/ui/Input';
import { MatchRecordingsCard } from '../components/site/MatchRecordingsCard';
import { SiteConfigEditor } from '../components/site/SiteConfigEditor';
import { siteProblems, tidySite, type SiteConfig } from '../lib/siteConfig';
import { Modal } from '../components/ui/Modal';
import { Toggle } from '../components/ui/Toggle';
import { CopyButton } from '../components/ui/CopyButton';

function settingValue(settings: { key: string; value: Record<string, unknown> }[] | undefined, key: string): Record<string, unknown> {
  return settings?.find((s) => s.key === key)?.value ?? {};
}

export function SettingsPage() {
  const { data: settings } = useSettings();
  const updateSettings = useUpdateSettings();
  const canEdit = usePermission('settings:edit');
  const pushToast = useUiStore((s) => s.pushToast);

  const s3 = settingValue(settings, 's3');
  const robotEvents = settingValue(settings, 'robot_events_api');
  const predictor = settingValue(settings, 'predictor');
  const chromaKey = settingValue(settings, 'chroma_key_defaults');
  // The backend reads/seeds this under `notifications` (backend/core/ntfy.py,
  // migration c3d4e5f6a7b8) — it holds the ntfy keys plus the team-alert
  // knobs (migration e1f2a3b4c5d6). PUT replaces the whole value, so `save()`
  // spreads the existing object to keep keys this page doesn't edit.
  const notifications = settingValue(settings, 'notifications');
  const site = settingValue(settings, 'site');
  const overlaySettings = settingValue(settings, 'overlay');

  const [s3Endpoint, setS3Endpoint] = useState('');
  const [s3PublicEndpoint, setS3PublicEndpoint] = useState('');
  const [s3Bucket, setS3Bucket] = useState('');
  const [s3AccessKey, setS3AccessKey] = useState('');
  const [s3SecretKey, setS3SecretKey] = useState('');
  const [s3Region, setS3Region] = useState('');
  const [reToken, setReToken] = useState('');
  const [threshold, setThreshold] = useState(15);
  const [ckColour, setCkColour] = useState('#00B140');
  const [ckSimilarity, setCkSimilarity] = useState(0.1);
  const [ckBlend, setCkBlend] = useState(0.05);
  const [ntfyServerUrl, setNtfyServerUrl] = useState('');
  const [ntfyTopic, setNtfyTopic] = useState('');
  const [ntfyEnabled, setNtfyEnabled] = useState(false);
  // The site form edits a draft; it is only saved once something changed.
  const [siteDraft, setSiteDraft] = useState<SiteConfig>({});
  const [siteDirty, setSiteDirty] = useState(false);
  const [teamAlerts, setTeamAlerts] = useState(true);
  const [leadMatches, setLeadMatches] = useState(3);
  const [leadMinutes, setLeadMinutes] = useState(15);
  const [scoresEnabled, setScoresEnabled] = useState(true);
  const [scoresExcluded, setScoresExcluded] = useState('F');
  const [showRobotName, setShowRobotName] = useState(false);
  const [lateMinutes, setLateMinutes] = useState(5);
  const [agendaEnabled, setAgendaEnabled] = useState(true);
  const [agendaLead, setAgendaLead] = useState(10);
  const [eventTimezone, setEventTimezone] = useState('UTC');

  useEffect(() => {
    setS3Endpoint(String(s3.endpoint_url ?? ''));
    setS3PublicEndpoint(String(s3.public_endpoint_url ?? ''));
    setS3Bucket(String(s3.bucket ?? ''));
    setS3AccessKey(String(s3.access_key ?? ''));
    setS3SecretKey(String(s3.secret_key ?? ''));
    setS3Region(String(s3.region ?? ''));
    setReToken(String(robotEvents.token ?? ''));
    setThreshold(Number(predictor.high_potential_threshold_pct ?? 15));
    setCkColour(String(chromaKey.colour ?? '#00B140'));
    setCkSimilarity(Number(chromaKey.similarity ?? 0.1));
    setCkBlend(Number(chromaKey.blend ?? 0.05));
    setNtfyServerUrl(String(notifications.server_url ?? ''));
    setNtfyTopic(String(notifications.topic ?? ''));
    setNtfyEnabled(Boolean(notifications.enabled ?? false));
    setSiteDraft(site as SiteConfig);
    setSiteDirty(false);
    setTeamAlerts(Boolean(notifications.team_alerts_enabled ?? true));
    setLeadMatches(Number(notifications.lead_matches_x ?? 3));
    setLeadMinutes(Number(notifications.lead_minutes_y ?? 15));
    setScoresEnabled(Boolean(notifications.scores_enabled ?? true));
    setShowRobotName(Boolean(overlaySettings.show_robot_name ?? false));
    setLateMinutes(Number(notifications.late_minutes ?? 5));
    setAgendaEnabled(Boolean(notifications.agenda_announcements_enabled ?? true));
    setAgendaLead(Number(notifications.agenda_lead_minutes ?? 10));
    setEventTimezone(String(notifications.timezone ?? 'UTC'));
    setScoresExcluded(
      Array.isArray(notifications.scores_excluded_rounds) ? notifications.scores_excluded_rounds.join(', ') : 'F',
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings]);

  function save() {
    let siteValue: Record<string, unknown> | null = null;
    if (siteDirty) {
      const problems = siteProblems(siteDraft);
      if (problems.length) {
        pushToast(`Public site settings: ${problems[0]}`, 'error');
        return;
      }
      siteValue = tidySite(siteDraft);
    }
    updateSettings.mutate(
      {
        ...(siteValue ? { site: { value: siteValue } } : {}),
        s3: {
          value: {
            endpoint_url: s3Endpoint,
            public_endpoint_url: s3PublicEndpoint,
            bucket: s3Bucket,
            access_key: s3AccessKey,
            secret_key: s3SecretKey,
            region: s3Region,
          },
        },
        robot_events_api: { value: { token: reToken } },
        predictor: { value: { high_potential_threshold_pct: threshold } },
        chroma_key_defaults: { value: { colour: ckColour, similarity: ckSimilarity, blend: ckBlend } },
        overlay: { value: { ...overlaySettings, show_robot_name: showRobotName } },
        notifications: {
          value: {
            ...notifications,
            server_url: ntfyServerUrl,
            topic: ntfyTopic,
            enabled: ntfyEnabled,
            team_alerts_enabled: teamAlerts,
            lead_matches_x: Math.max(0, Math.min(20, Math.round(leadMatches) || 0)),
            lead_minutes_y: Math.max(0, Math.min(120, Math.round(leadMinutes) || 0)),
            scores_enabled: scoresEnabled,
            late_minutes: Math.max(0, Math.min(60, Math.round(lateMinutes) || 0)),
            agenda_announcements_enabled: agendaEnabled,
            agenda_lead_minutes: Math.max(0, Math.min(240, Math.round(agendaLead) || 0)),
            timezone: eventTimezone.trim() || 'UTC',
            scores_excluded_rounds: scoresExcluded
              .split(/[\s,;]+/)
              .map((r) => r.trim().toUpperCase())
              .filter(Boolean),
          },
        },
      },
      {
        onSuccess: () => pushToast('Settings saved', 'success'),
        onError: (err) => pushToast(err instanceof Error ? err.message : 'Save failed', 'error'),
      },
    );
  }

  return (
    <div>
      <PageHeader title="Settings" subtitle="S3 storage, Robot Events, predictor, chroma key defaults, API keys." />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">S3 Media Storage</h2>
          <div className="space-y-2">
            <div>
              <Label>Endpoint URL</Label>
              <Input value={s3Endpoint} onChange={(e) => setS3Endpoint(e.target.value)} disabled={!canEdit} />
            </div>
            <div>
              <Label>Public Endpoint URL (optional)</Label>
              <Input
                placeholder="Defaults to Endpoint URL"
                value={s3PublicEndpoint}
                onChange={(e) => setS3PublicEndpoint(e.target.value)}
                disabled={!canEdit}
              />
              <p className="mt-1 text-xs text-vmd-textSubtle">
                Only used to sign presigned video URLs. Set this if Endpoint URL is an
                internal/in-cluster hostname (e.g. MinIO) a browser can't resolve.
              </p>
            </div>
            <div>
              <Label>Bucket</Label>
              <Input value={s3Bucket} onChange={(e) => setS3Bucket(e.target.value)} disabled={!canEdit} />
            </div>
            <div>
              <Label>Access Key</Label>
              <Input value={s3AccessKey} onChange={(e) => setS3AccessKey(e.target.value)} disabled={!canEdit} />
            </div>
            <div>
              <Label>Secret Key</Label>
              <Input type="password" value={s3SecretKey} onChange={(e) => setS3SecretKey(e.target.value)} disabled={!canEdit} />
            </div>
            <div>
              <Label>Region</Label>
              <Input value={s3Region} onChange={(e) => setS3Region(e.target.value)} disabled={!canEdit} />
            </div>
          </div>
        </Card>

        <Card>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">Robot Events API</h2>
          <Label>API Token</Label>
          <Input type="password" value={reToken} onChange={(e) => setReToken(e.target.value)} disabled={!canEdit} />

          <h2 className="mb-3 mt-5 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">
            AI Predictor
          </h2>
          <Label>High-potential threshold ({threshold}%)</Label>
          <input
            type="range"
            min={0}
            max={100}
            value={threshold}
            onChange={(e) => setThreshold(Number(e.target.value))}
            disabled={!canEdit}
            className="w-full"
          />

          <h2 className="mb-3 mt-5 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">
            Chroma Key Defaults
          </h2>
          <div className="grid grid-cols-3 gap-3">
            <div>
              <Label>Colour</Label>
              <input
                type="color"
                value={ckColour}
                onChange={(e) => setCkColour(e.target.value)}
                disabled={!canEdit}
                className="h-9 w-full rounded-lg border border-vmd-border bg-vmd-elevated"
              />
            </div>
            <div>
              <Label>Similarity ({ckSimilarity.toFixed(2)})</Label>
              <input
                type="range"
                min={0}
                max={1}
                step={0.01}
                value={ckSimilarity}
                onChange={(e) => setCkSimilarity(Number(e.target.value))}
                disabled={!canEdit}
                className="w-full"
              />
            </div>
            <div>
              <Label>Blend ({ckBlend.toFixed(2)})</Label>
              <input
                type="range"
                min={0}
                max={1}
                step={0.01}
                value={ckBlend}
                onChange={(e) => setCkBlend(Number(e.target.value))}
                disabled={!canEdit}
                className="w-full"
              />
            </div>
          </div>
        </Card>

        <Card className="lg:col-span-2">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">Public website (/site)</h2>
          <SiteConfigEditor
            value={siteDraft}
            onChange={(next) => {
              setSiteDraft(next);
              setSiteDirty(true);
            }}
            disabled={!canEdit}
          />
        </Card>

        <MatchRecordingsCard canEdit={canEdit} />

        <Card>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">Stream overlay</h2>
          <div className="flex items-center justify-between">
            <Label>Show robot names on the overlay</Label>
            <Toggle checked={showRobotName} onChange={setShowRobotName} disabled={!canEdit} />
          </div>
          <p className="mt-2 text-xs text-vmd-textSubtle">
            Off by default: robot names need VEX Events approval before they go on stream. Overlay pages also accept{' '}
            <code>?robot_name=1</code> to force it for a single browser source.
          </p>
        </Card>

        <Card>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">
            Team alerts (room displays &amp; phones)
          </h2>
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label>Enabled</Label>
              <Toggle checked={teamAlerts} onChange={setTeamAlerts} disabled={!canEdit} />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label>Alert when N matches away (0 = off)</Label>
                <Input
                  type="number"
                  min={0}
                  max={20}
                  value={leadMatches}
                  onChange={(e) => setLeadMatches(Number(e.target.value))}
                  disabled={!canEdit}
                />
              </div>
              <div>
                <Label>Alert N minutes before (0 = off)</Label>
                <Input
                  type="number"
                  min={0}
                  max={120}
                  value={leadMinutes}
                  onChange={(e) => setLeadMinutes(Number(e.target.value))}
                  disabled={!canEdit}
                />
              </div>
            </div>
            <div>
              <Label>Late warning after N minutes overdue (0 = off)</Label>
              <Input
                type="number"
                min={0}
                max={60}
                value={lateMinutes}
                onChange={(e) => setLateMinutes(Number(e.target.value))}
                disabled={!canEdit}
              />
            </div>
            <div className="flex items-center justify-between">
              <Label>Announce agenda items (lunch, finals…) to all rooms</Label>
              <Toggle checked={agendaEnabled} onChange={setAgendaEnabled} disabled={!canEdit} />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label>Default announcement lead (minutes)</Label>
                <Input
                  type="number"
                  min={0}
                  max={240}
                  value={agendaLead}
                  onChange={(e) => setAgendaLead(Number(e.target.value))}
                  disabled={!canEdit}
                />
              </div>
              <div>
                <Label>Event timezone (IANA, e.g. Europe/London)</Label>
                <Input value={eventTimezone} onChange={(e) => setEventTimezone(e.target.value)} disabled={!canEdit} />
              </div>
            </div>
            <div className="flex items-center justify-between">
              <Label>Send match scores once submitted</Label>
              <Toggle checked={scoresEnabled} onChange={setScoresEnabled} disabled={!canEdit} />
            </div>
            <div>
              <Label>Rounds without score alerts (TM round codes)</Label>
              <Input
                placeholder="F"
                value={scoresExcluded}
                onChange={(e) => setScoresExcluded(e.target.value)}
                disabled={!canEdit}
              />
            </div>
            <p className="text-xs text-vmd-textSubtle">
              Both match-count and time alerts fire when enabled. Rooms and rooms&apos; QR codes are managed on the Rooms page.
              Finals are <code>F</code>; others: <code>QUAL</code>, <code>R16</code>, <code>QF</code>, <code>SF</code>.
            </p>
          </div>
        </Card>

        <Card>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">
            Staff alerts (ntfy)
          </h2>
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label>Enabled</Label>
              <Toggle checked={ntfyEnabled} onChange={setNtfyEnabled} disabled={!canEdit} />
            </div>
            <div>
              <Label>Server URL</Label>
              <Input
                placeholder="https://ntfy.sh"
                value={ntfyServerUrl}
                onChange={(e) => setNtfyServerUrl(e.target.value)}
                disabled={!canEdit}
              />
            </div>
            <div>
              <Label>Topic</Label>
              <Input value={ntfyTopic} onChange={(e) => setNtfyTopic(e.target.value)} disabled={!canEdit} />
            </div>
            <p className="text-xs text-vmd-textSubtle">
              Fires on integration DEGRADED, leader failover, automation retry-exhaustion, and video
              processing failure (Appendix A.8).
            </p>
          </div>
        </Card>
      </div>

      {canEdit && (
        <Button variant="primary" className="mt-4" onClick={save} disabled={updateSettings.isPending}>
          Save Settings
        </Button>
      )}

      <div className="mt-8">
        <TeleprompterIndexSection canEdit={canEdit} />
      </div>

      <div className="mt-8">
        <ApiKeysSection canEdit={canEdit} />
      </div>
    </div>
  );
}

/** Appendix B.4 / AUDIT_FINDINGS.md 1.3: the `/prompter` bare index route is
 * gated on a `prompter_index_token` system setting. This section shows the
 * current token with copy-to-clipboard, and a "Regenerate" button that
 * writes a freshly-generated token through the same generic
 * `PUT /api/v1/settings` mechanism every other section uses — there's no
 * dedicated rotation endpoint, but the generic settings PUT already
 * supports replacing the value, which is all rotation requires. Written
 * defensively since the `prompter_index_token` backend key is landing in
 * parallel and may not exist yet. */
function TeleprompterIndexSection({ canEdit }: { canEdit: boolean }) {
  const { data: settings, isLoading } = useSettings();
  const updateSettings = useUpdateSettings();
  const pushToast = useUiStore((s) => s.pushToast);

  const tokenValue = settingValue(settings, 'prompter_index_token');
  const token = String(tokenValue.token ?? '');

  function regenerate() {
    const fresh =
      typeof crypto !== 'undefined' && 'randomUUID' in crypto
        ? crypto.randomUUID().replace(/-/g, '')
        : Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
    updateSettings.mutate(
      { prompter_index_token: { value: { token: fresh } } },
      {
        onSuccess: () => pushToast('Teleprompter index token regenerated', 'success'),
        onError: (err) => pushToast(err instanceof Error ? err.message : 'Regenerate failed', 'error'),
      },
    );
  }

  return (
    <div>
      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">
        Teleprompter Index
      </h2>
      <Card>
        <p className="mb-2 text-xs text-vmd-textSubtle">
          Gates the bare <code>/prompter</code> index page listing all active timer instances.
        </p>
        {isLoading ? (
          <p className="text-sm text-vmd-textSubtle">Loading…</p>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <code className="vmd-code-block break-all text-xs">{token || '(not set)'}</code>
            {token && <CopyButton value={token} />}
            {canEdit && (
              <Button variant="ghost" onClick={regenerate} disabled={updateSettings.isPending}>
                <RefreshCw size={14} /> Regenerate
              </Button>
            )}
          </div>
        )}
      </Card>
    </div>
  );
}

function ApiKeysSection({ canEdit }: { canEdit: boolean }) {
  const { data: keys } = useApiKeys();
  const createKey = useCreateApiKey();
  const revokeKey = useRevokeApiKey();
  const [modalOpen, setModalOpen] = useState(false);
  const [name, setName] = useState('');
  const [permissions, setPermissions] = useState<string[]>([]);
  const [createdKey, setCreatedKey] = useState<string | null>(null);

  function togglePermission(p: string) {
    setPermissions((prev) => (prev.includes(p) ? prev.filter((x) => x !== p) : [...prev, p]));
  }

  function handleCreate() {
    createKey.mutate(
      { name, permissions },
      {
        onSuccess: (res) => {
          setCreatedKey(res.raw_key);
          setName('');
          setPermissions([]);
        },
      },
    );
  }

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">API Keys</h2>
        {canEdit && (
          <Button variant="primary" onClick={() => setModalOpen(true)}>
            <Plus size={16} /> New API Key
          </Button>
        )}
      </div>
      <Card>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase text-vmd-textSubtle">
              <th className="pb-2">Name</th>
              <th className="pb-2">Permissions</th>
              <th className="pb-2">Last Used</th>
              <th className="pb-2">Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {keys?.map((key) => (
              <tr key={key.id} className="border-t border-vmd-border">
                <td className="py-1.5">{key.name}</td>
                <td className="py-1.5 text-xs text-vmd-textMuted">{key.permissions.join(', ') || '(none)'}</td>
                <td className="py-1.5 text-xs text-vmd-textSubtle">
                  {key.last_used_at ? new Date(key.last_used_at).toLocaleString() : 'never'}
                </td>
                <td className="py-1.5">{key.revoked ? 'Revoked' : 'Active'}</td>
                <td className="py-1.5 text-right">
                  {canEdit && !key.revoked && (
                    <button onClick={() => revokeKey.mutate(key.id)} className="text-vmd-textSubtle hover:text-vmd-danger">
                      <Trash2 size={14} />
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <Modal
        open={modalOpen}
        onClose={() => {
          setModalOpen(false);
          setCreatedKey(null);
        }}
        title="New API Key"
      >
        {createdKey ? (
          <div>
            <p className="mb-2 text-sm text-vmd-danger">
              This key is shown only once — copy it now. It cannot be retrieved again.
            </p>
            <p className="vmd-code-block break-all text-xs">{createdKey}</p>
          </div>
        ) : (
          <div className="space-y-3">
            <div>
              <Label>Name</Label>
              <Input value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div>
              <Label>Permissions</Label>
              <div className="grid grid-cols-2 gap-1 text-sm">
                {ALL_PERMISSIONS.map((p) => (
                  <label key={p} className="flex items-center gap-1.5">
                    <input type="checkbox" checked={permissions.includes(p)} onChange={() => togglePermission(p)} />
                    {p}
                  </label>
                ))}
              </div>
            </div>
            <Button variant="primary" onClick={handleCreate} disabled={!name || createKey.isPending}>
              Create
            </Button>
          </div>
        )}
      </Modal>
    </div>
  );
}
