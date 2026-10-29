import { useState } from 'react';
import { Pause, Play, Square } from 'lucide-react';
import { useTimerControl, type Audience } from '../../api/comms';
import { useUiStore } from '../../stores/ui';
import { Button } from '../ui/Button';
import { Card } from '../ui/Card';
import { Input, Label, Select } from '../ui/Input';
import type { TimerRequest } from '../../types/api';

const QUICK_MINUTES = [1, 2, 5, 10, 15, 30];

/** Stagetimer-style cue for the iPads: a colour-coded countdown (green →
 * amber → red → flashing overtime) or a count-up, with pause / resume /
 * ±1 min and stop. The timer runs on the server clock, so every iPad shows
 * the same time. */
export function TimerCard({ audience }: { audience: Audience }) {
  const control = useTimerControl();
  const pushToast = useUiStore((s) => s.pushToast);
  const [mode, setMode] = useState<'countdown' | 'countup'>('countdown');
  const [minutes, setMinutes] = useState(5);
  const [seconds, setSeconds] = useState(0);
  const [label, setLabel] = useState('');
  const [warn, setWarn] = useState(120);
  const [critical, setCritical] = useState(30);

  const duration = Math.max(0, Math.round(minutes) || 0) * 60 + Math.max(0, Math.min(59, Math.round(seconds) || 0));
  const cuesOk = critical <= warn;
  const canStart = (mode === 'countup' || duration >= 1) && cuesOk;

  function run(body: Omit<TimerRequest, 'stations' | 'roles'>, done?: string) {
    control.mutate(
      { ...audience, ...body },
      {
        onSuccess: (r) => done && pushToast(`${done} (${r.stations.length} station${r.stations.length === 1 ? '' : 's'})`, 'success'),
        onError: (err) => pushToast(err instanceof Error ? err.message : 'Request failed', 'error'),
      },
    );
  }

  return (
    <Card className="mb-4" data-testid="timer-card">
      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">Timer</h2>
      <div className="grid gap-3 md:grid-cols-4">
        <div>
          <Label>Type</Label>
          <Select value={mode} onChange={(e) => setMode(e.target.value as 'countdown' | 'countup')} aria-label="Timer type">
            <option value="countdown">Countdown</option>
            <option value="countup">Count up</option>
          </Select>
        </div>
        {mode === 'countdown' && (
          <div className="flex items-end gap-2">
            <div className="flex-1">
              <Label>Minutes</Label>
              <Input type="number" min={0} max={1440} value={minutes} onChange={(e) => setMinutes(Number(e.target.value))} aria-label="Minutes" />
            </div>
            <div className="flex-1">
              <Label>Seconds</Label>
              <Input type="number" min={0} max={59} value={seconds} onChange={(e) => setSeconds(Number(e.target.value))} aria-label="Seconds" />
            </div>
          </div>
        )}
        <div className="md:col-span-2">
          <Label>Label shown above the clock</Label>
          <Input maxLength={60} placeholder="Finals break" value={label} onChange={(e) => setLabel(e.target.value)} aria-label="Timer label" />
        </div>
      </div>
      {mode === 'countdown' && (
        <>
          <div className="mt-3 flex flex-wrap gap-1.5">
            {QUICK_MINUTES.map((m) => (
              <Button key={m} variant="ghost" onClick={() => { setMinutes(m); setSeconds(0); }}>
                {m} min
              </Button>
            ))}
          </div>
          <div className="mt-3 grid gap-3 md:grid-cols-4">
            <div>
              <Label>Amber at (seconds left)</Label>
              <Input type="number" min={0} value={warn} onChange={(e) => setWarn(Number(e.target.value))} aria-label="Amber at" />
            </div>
            <div>
              <Label>Red at (seconds left)</Label>
              <Input type="number" min={0} value={critical} onChange={(e) => setCritical(Number(e.target.value))} aria-label="Red at" />
            </div>
            {!cuesOk && <p className="self-end text-xs text-vmd-danger">Red must not come before amber.</p>}
          </div>
        </>
      )}
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Button
          variant="primary"
          disabled={!canStart || control.isPending}
          onClick={() =>
            run(
              {
                action: 'start',
                mode,
                label: label.trim(),
                ...(mode === 'countdown' ? { duration_seconds: duration, warn_seconds: warn, critical_seconds: critical } : {}),
              },
              'Timer started',
            )
          }
        >
          <Play size={14} /> Start
        </Button>
        <Button variant="secondary" onClick={() => run({ action: 'pause' }, 'Paused')}>
          <Pause size={14} /> Pause
        </Button>
        <Button variant="secondary" onClick={() => run({ action: 'resume' }, 'Resumed')}>
          <Play size={14} /> Resume
        </Button>
        <Button variant="secondary" onClick={() => run({ action: 'adjust', adjust_seconds: 60 }, '+1 min')}>
          +1 min
        </Button>
        <Button variant="secondary" onClick={() => run({ action: 'adjust', adjust_seconds: -60 }, '−1 min')}>
          −1 min
        </Button>
        <Button variant="danger" onClick={() => run({ action: 'stop' }, 'Timer stopped')}>
          <Square size={14} /> Stop
        </Button>
      </div>
    </Card>
  );
}
