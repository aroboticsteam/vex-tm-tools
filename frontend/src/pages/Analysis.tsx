import { useEffect, useMemo, useState } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import {
  useCaptureRecordings,
  useCaptureStatus,
  useMarkMoment,
  useReplayControl,
  useReplayPlay,
  useReplayState,
} from '../api/capture';
import { useCallIntegrationService, useIntegrations } from '../api/integrations';
import { useTeams } from '../api/teams';
import { usePermission } from '../hooks/usePermission';
import { useUiStore } from '../stores/ui';
import {
  DEFAULT_SETUP,
  MARK_LENGTHS,
  SPEEDS,
  findByNumber,
  loadSetup,
  momentPlayable,
  orderRecordings,
  recordingLabel,
  saveSetup,
  stepMatch,
  type DeskSetup,
} from '../lib/analysis';
import { Button } from '../components/ui/Button';
import { Card, PageHeader } from '../components/ui/Card';
import { Chip } from '../components/ui/Badge';
import { Input, Label, Select } from '../components/ui/Input';
import type { CaptureRecording } from '../types/api';

type Tab = 'stats' | 'match';

export function AnalysisPage() {
  const canView = usePermission('analysis:view');
  if (!canView) return <p className="text-sm text-vmd-textSubtle">You do not have access to the analysis desk.</p>;
  return <AnalysisDesk />;
}

function AnalysisDesk() {
  const canControl = usePermission('capture:control');
  const [tab, setTab] = useState<Tab>('match');
  const [setup, setSetup] = useState<DeskSetup>(() => loadSetup());
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const { data: recordings = [] } = useCaptureRecordings({ limit: 300 });
  const ordered = useMemo(() => orderRecordings(recordings), [recordings]);

  const updateSetup = (patch: Partial<DeskSetup>) => {
    const next = { ...setup, ...patch };
    setSetup(next);
    saveSetup(next);
  };

  // Follow the live recording unless the operator has picked another match.
  const { data: status } = useCaptureStatus();
  const liveId = status?.recording?.id ?? null;
  useEffect(() => {
    if (liveId && !selectedId) setSelectedId(liveId);
  }, [liveId, selectedId]);
  const selected = ordered.find((r) => r.id === selectedId) ?? null;

  return (
    <div>
      <PageHeader title="Analysis Desk" subtitle="Match stats, instant-replay marks and replay playout" />
      <div className="mb-4 flex gap-2" role="tablist">
        {(['match', 'stats'] as const).map((t) => (
          <Button key={t} role="tab" aria-selected={tab === t} variant={tab === t ? 'primary' : 'secondary'} onClick={() => setTab(t)}>
            {t === 'match' ? 'Match' : 'Stats'}
          </Button>
        ))}
      </div>
      <MatchNav ordered={ordered} selected={selected} onSelect={setSelectedId} />
      {tab === 'stats' ? (
        <StatsTab recording={selected} />
      ) : (
        <MatchTab recording={selected} setup={setup} onSetup={updateSetup} canControl={canControl} agentConnected={!!status?.connected} recordingNow={status?.recording ?? null} />
      )}
    </div>
  );
}

function MatchNav({
  ordered,
  selected,
  onSelect,
}: {
  ordered: CaptureRecording[];
  selected: CaptureRecording | null;
  onSelect: (id: string | null) => void;
}) {
  const [query, setQuery] = useState('');
  const go = (r: CaptureRecording | null) => r && onSelect(r.id);
  return (
    <Card className="mb-4 flex flex-wrap items-center gap-2">
      <Button aria-label="Previous match" onClick={() => go(stepMatch(ordered, selected?.id ?? null, -1))}>
        <ChevronLeft size={16} />
      </Button>
      <span className="min-w-24 text-center text-lg font-semibold text-vmd-textStrong" data-testid="current-match">
        {selected ? recordingLabel(selected) : 'No match'}
      </span>
      <Button aria-label="Next match" onClick={() => go(stepMatch(ordered, selected?.id ?? null, 1))}>
        <ChevronRight size={16} />
      </Button>
      <form
        className="ml-4 flex items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          go(findByNumber(ordered, query));
        }}
      >
        <Input aria-label="Go to match number" placeholder="Match #" value={query} onChange={(e) => setQuery(e.target.value)} className="w-28" />
        <Button type="submit">Go</Button>
      </form>
      {selected && (
        <span className="ml-auto flex gap-1">
          {selected.teams.map((t) => (
            <Chip key={t}>{t}</Chip>
          ))}
        </span>
      )}
    </Card>
  );
}

function StatsTab({ recording }: { recording: CaptureRecording | null }) {
  const { data: teams = [] } = useTeams();
  if (!recording) return <p className="text-sm text-vmd-textSubtle">Select a match to see its teams.</p>;
  // Stats only: we never show predicted scores or win probabilities here.
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      {recording.teams.map((number) => {
        const team = teams.find((t) => t.team_number.toUpperCase() === number.toUpperCase());
        const stats = team?.cached_stats ?? {};
        return (
          <Card key={number} aria-label={`Stats for ${number}`}>
            <h2 className="text-lg font-semibold text-vmd-textStrong">{number}</h2>
            <p className="mb-2 text-sm text-vmd-textMuted">{team?.robot_name ?? 'No robot name'}</p>
            {Object.keys(stats).length === 0 ? (
              <p className="text-sm text-vmd-textSubtle">No cached stats yet.</p>
            ) : (
              <dl className="grid grid-cols-2 gap-1 text-sm">
                {Object.entries(stats).map(([key, value]) => (
                  <div key={key} className="contents">
                    <dt className="text-vmd-textMuted">{key}</dt>
                    <dd className="text-right text-vmd-text">{String(value)}</dd>
                  </div>
                ))}
              </dl>
            )}
          </Card>
        );
      })}
    </div>
  );
}

function MatchTab({
  recording,
  setup,
  onSetup,
  canControl,
  agentConnected,
  recordingNow,
}: {
  recording: CaptureRecording | null;
  setup: DeskSetup;
  onSetup: (patch: Partial<DeskSetup>) => void;
  canControl: boolean;
  agentConnected: boolean;
  recordingNow: CaptureRecording | null;
}) {
  const pushToast = useUiStore((s) => s.pushToast);
  const mark = useMarkMoment();
  const play = useReplayPlay(setup.replayId);
  const control = useReplayControl(setup.replayId);
  const { data: replay } = useReplayState(setup.replayId);
  const callService = useCallIntegrationService();
  const { data: integrations = [] } = useIntegrations();
  const obsList = integrations.filter((i) => i.domain === 'obs');
  const wledList = integrations.filter((i) => i.domain === 'wled');
  const [speed, setSpeed] = useState<number>(0.5);
  const isRecording = !!recordingNow;

  const doMark = (seconds: number) =>
    mark.mutate(seconds, {
      onSuccess: () => pushToast(`Marked last ${seconds}s`, 'success'),
      onError: (err) => pushToast(err instanceof Error ? err.message : 'Mark failed', 'error'),
    });

  // Keyboard marks (ignored while typing in a field).
  useEffect(() => {
    if (!canControl) return undefined;
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || e.metaKey || e.ctrlKey || e.altKey) return;
      const seconds = setup.keys[e.key];
      if (seconds) {
        e.preventDefault();
        mark.mutate(seconds, {
          onSuccess: () => pushToast(`Marked last ${seconds}s`, 'success'),
          onError: (err) => pushToast(err instanceof Error ? err.message : 'Mark failed', 'error'),
        });
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canControl, setup.keys]);

  const sendToProgram = (kind: 'moment' | 'recording', id: string) => {
    play.mutate(
      { kind, id, rate: speed },
      {
        onSuccess: () => {
          if (setup.obsEntity && setup.replayScene) {
            callService.mutate({ entityId: setup.obsEntity, service: 'switch_scene', data: { scene_name: setup.replayScene } });
          }
        },
        onError: (err) => pushToast(err instanceof Error ? err.message : 'Could not send to program', 'error'),
      },
    );
  };

  const backToProgram = () => {
    control.mutate({ action: 'stop' });
    if (setup.obsEntity && setup.programScene) {
      callService.mutate({ entityId: setup.obsEntity, service: 'switch_scene', data: { scene_name: setup.programScene } });
    }
  };

  const flashLed = () => {
    if (!setup.wledEntity) return;
    callService.mutate({ entityId: setup.wledEntity, service: 'flash', data: {} });
  };

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Card>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">Capture</h2>
        <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
          <Chip>{agentConnected ? 'Agent connected' : 'Agent offline'}</Chip>
          <Chip>{isRecording ? `Recording ${recordingLabel(recordingNow!)}` : 'Not recording'}</Chip>
        </div>
        <div className="flex flex-wrap gap-2">
          {MARK_LENGTHS.map((s) => (
            <Button key={s} variant="primary" disabled={!canControl || !isRecording || mark.isPending} onClick={() => doMark(s)}>
              Mark last {s}s
            </Button>
          ))}
        </div>
        <p className="mt-2 text-xs text-vmd-textSubtle">
          Keys: {Object.entries(setup.keys).map(([k, v]) => `${k} = ${v}s`).join(', ')}
        </p>
        <h2 className="mb-2 mt-5 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">Moments</h2>
        {!recording || recording.moments.length === 0 ? (
          <p className="text-sm text-vmd-textSubtle">No moments marked for this match.</p>
        ) : (
          <ul className="space-y-2">
            {recording.moments.map((m) => (
              <li key={m.id} className="flex items-center gap-2 text-sm" aria-label={`Moment ${m.number}`}>
                <span className="w-24 text-vmd-text">Moment {m.number}</span>
                <span className="text-vmd-textMuted">{m.length_s}s</span>
                <Chip>{m.status === 'READY' ? 'Ready' : m.status === 'ERROR' ? 'Failed' : 'Uploading…'}</Chip>
                <Button
                  className="ml-auto"
                  disabled={!canControl || !momentPlayable(m.status) || play.isPending}
                  onClick={() => sendToProgram('moment', m.id)}
                  aria-label={`Send moment ${m.number} to program`}
                >
                  Send to program
                </Button>
              </li>
            ))}
          </ul>
        )}
        {recording && recording.status === 'READY' && (
          <Button className="mt-3" disabled={!canControl || play.isPending} onClick={() => sendToProgram('recording', recording.id)}>
            Send full match to program
          </Button>
        )}
      </Card>

      <Card>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">Replay player</h2>
        <p className="mb-2 text-sm text-vmd-textMuted">
          {replay?.playing ? `Playing: ${replay.label ?? ''}${replay.paused ? ' (paused)' : ''}` : 'Nothing playing'}
        </p>
        <div className="mb-3 flex flex-wrap gap-2">
          {SPEEDS.map((s) => (
            <Button
              key={s}
              variant={speed === s ? 'primary' : 'secondary'}
              aria-pressed={speed === s}
              disabled={!canControl}
              onClick={() => {
                setSpeed(s);
                if (replay?.playing) control.mutate({ action: 'rate', value: s });
              }}
            >
              {s}×
            </Button>
          ))}
        </div>
        <div className="flex flex-wrap gap-2">
          <Button disabled={!canControl || !replay?.playing} onClick={() => control.mutate({ action: replay?.paused ? 'resume' : 'pause' })}>
            {replay?.paused ? 'Resume' : 'Pause'}
          </Button>
          <Button disabled={!canControl || !replay?.playing} onClick={() => control.mutate({ action: 'restart' })}>
            Restart
          </Button>
          <Button variant="primary" disabled={!canControl} onClick={backToProgram}>
            Back to program
          </Button>
          <Button disabled={!canControl || !setup.wledEntity} onClick={flashLed}>
            Flash LED
          </Button>
        </div>

        <h2 className="mb-2 mt-5 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">Setup</h2>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <Label>Replay player id</Label>
            <Input aria-label="Replay player id" value={setup.replayId} onChange={(e) => onSetup({ replayId: e.target.value.trim() || DEFAULT_SETUP.replayId })} />
          </div>
          <div>
            <Label>OBS instance</Label>
            <Select aria-label="OBS instance" value={setup.obsEntity} onChange={(e) => onSetup({ obsEntity: e.target.value })}>
              <option value="">None</option>
              {obsList.map((i) => (
                <option key={i.entity_id} value={i.entity_id}>
                  {i.display_name}
                </option>
              ))}
            </Select>
          </div>
          <div>
            <Label>Replay scene</Label>
            <Input aria-label="Replay scene" value={setup.replayScene} onChange={(e) => onSetup({ replayScene: e.target.value })} />
          </div>
          <div>
            <Label>Program scene (back to)</Label>
            <Input aria-label="Program scene" value={setup.programScene} onChange={(e) => onSetup({ programScene: e.target.value })} />
          </div>
          <div>
            <Label>WLED instance</Label>
            <Select aria-label="WLED instance" value={setup.wledEntity} onChange={(e) => onSetup({ wledEntity: e.target.value })}>
              <option value="">None</option>
              {wledList.map((i) => (
                <option key={i.entity_id} value={i.entity_id}>
                  {i.display_name}
                </option>
              ))}
            </Select>
          </div>
        </div>
        <p className="mt-2 text-xs text-vmd-textSubtle">
          Add <code>/replay/{setup.replayId}</code> as a 1920×1080 browser source in the replay scene.
        </p>
      </Card>
    </div>
  );
}
