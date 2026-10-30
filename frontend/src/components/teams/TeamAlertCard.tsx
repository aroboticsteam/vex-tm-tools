import { useState } from 'react';
import { BellRing } from 'lucide-react';
import { useTeamAlert } from '../../api/notifications';
import { ALERT_REASONS, alertSummary, parseTeams } from '../../lib/teams';
import { useUiStore } from '../../stores/ui';
import { Button } from '../ui/Button';
import { Card } from '../ui/Card';
import { Input, Label, Select, Textarea } from '../ui/Input';
import type { TeamAlertReason } from '../../types/api';

/** Staff alert addressed to specific teams (issue #44). */
export function TeamAlertCard({ initialTeams = '' }: { initialTeams?: string }) {
  const alert = useTeamAlert();
  const pushToast = useUiStore((s) => s.pushToast);
  const [teamsText, setTeamsText] = useState(initialTeams);
  const [reason, setReason] = useState<TeamAlertReason>('pit_admin');
  const [message, setMessage] = useState('');
  const teams = parseTeams(teamsText);
  const customMissing = reason === 'custom' && !message.trim();

  function send() {
    alert.mutate(
      { teams, reason, message: message.trim() || null },
      {
        onSuccess: (res) => {
          const summary = alertSummary(res);
          pushToast(summary.text, summary.kind);
          setMessage('');
        },
        onError: (err) => pushToast(err instanceof Error ? err.message : 'Alert failed', 'error'),
      },
    );
  }

  return (
    <Card className="mt-6">
      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">Alert teams</h2>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="space-y-2">
          <div>
            <Label>Teams (comma or space separated)</Label>
            <Input aria-label="Teams to alert" value={teamsText} placeholder="1234A, 5678B" onChange={(e) => setTeamsText(e.target.value)} />
          </div>
          <div>
            <Label>Reason</Label>
            <Select aria-label="Alert reason" value={reason} onChange={(e) => setReason(e.target.value as TeamAlertReason)}>
              {(Object.keys(ALERT_REASONS) as TeamAlertReason[]).map((r) => (
                <option key={r} value={r}>
                  {ALERT_REASONS[r]}
                </option>
              ))}
            </Select>
          </div>
        </div>
        <div>
          <Label>{reason === 'custom' ? 'Message' : 'Extra note (optional)'}</Label>
          <Textarea
            aria-label="Alert message"
            rows={3}
            maxLength={300}
            className="font-sans"
            value={message}
            onChange={(e) => setMessage(e.target.value)}
          />
          <Button variant="primary" className="mt-3" onClick={send} disabled={alert.isPending || teams.length === 0 || customMissing}>
            <BellRing size={14} /> Alert {teams.length || ''} team{teams.length === 1 ? '' : 's'}
          </Button>
        </div>
      </div>
    </Card>
  );
}
