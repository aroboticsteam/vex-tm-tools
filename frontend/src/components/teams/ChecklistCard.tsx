import { Check, Circle } from 'lucide-react';
import { useTeamChecklist, useUpdateTeam } from '../../api/teams';
import { usePermission } from '../../hooks/usePermission';
import { Card } from '../ui/Card';
import { Label, Select } from '../ui/Input';
import { INSPECTION_LABELS } from '../../lib/checklist';
import type { InspectionStatus } from '../../types/api';

export function ChecklistMark({ done, label }: { done: boolean; label: string }) {
  return (
    <span className={`inline-flex items-center gap-1 text-xs ${done ? 'text-vmd-success' : 'text-vmd-textSubtle'}`}>
      {done ? <Check size={12} aria-hidden /> : <Circle size={12} aria-hidden />}
      {label}
    </span>
  );
}

/** Team checklist (issue #43): inspection (staff-editable) plus the two videos. */
export function ChecklistCard({ teamNumber }: { teamNumber: string }) {
  const { data } = useTeamChecklist();
  const update = useUpdateTeam();
  const canEdit = usePermission('teams:edit');
  const row = data?.find((r) => r.team_number === teamNumber);
  if (!row) return null;

  return (
    <Card>
      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">
        Checklist ({row.done}/{row.total})
      </h2>
      <div className="space-y-3">
        <div>
          <Label>Inspection</Label>
          <Select
            aria-label="Inspection status"
            value={row.inspection}
            disabled={!canEdit || update.isPending}
            onChange={(e) =>
              update.mutate({ number: teamNumber, body: { inspection_status: e.target.value as InspectionStatus } })
            }
          >
            {(Object.keys(INSPECTION_LABELS) as InspectionStatus[]).map((s) => (
              <option key={s} value={s}>
                {INSPECTION_LABELS[s]}
              </option>
            ))}
          </Select>
        </div>
        <div className="flex flex-col gap-1">
          <ChecklistMark done={row.bot_video} label="360 robot video" />
          <ChecklistMark done={row.team_video} label="Team video" />
        </div>
      </div>
    </Card>
  );
}
