import { useCaptureRecordings } from '../../api/capture';
import { usePermission } from '../../hooks/usePermission';
import { recordingLabel, orderRecordings } from '../../lib/analysis';
import { Card } from '../ui/Card';

function formatDuration(seconds: number | null): string {
  if (seconds == null) return '';
  const m = Math.floor(seconds / 60);
  return `${m}:${String(Math.round(seconds % 60)).padStart(2, '0')}`;
}

/** Match recordings the capture agent tagged with this team (issue #42). */
export function TeamRecordingsCard({ teamNumber }: { teamNumber: string }) {
  const canView = usePermission('analysis:view');
  if (!canView) return null;
  return <RecordingsList teamNumber={teamNumber} />;
}

function RecordingsList({ teamNumber }: { teamNumber: string }) {
  const { data = [] } = useCaptureRecordings({ team: teamNumber, limit: 200 });
  const recordings = orderRecordings(data);

  return (
    <Card>
      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">
        Match recordings ({recordings.length})
      </h2>
      {recordings.length === 0 ? (
        <p className="text-sm text-vmd-textSubtle">No recordings for this team yet.</p>
      ) : (
        <ul className="space-y-1 text-sm">
          {recordings.map((r) => (
            <li key={r.id} className="flex items-center justify-between gap-2">
              <span className="text-vmd-text">{recordingLabel(r)}</span>
              {r.status === 'READY' ? (
                <a
                  className="text-vmd-link hover:underline"
                  href={`/api/v1/public/recordings/${r.id}/video`}
                  target="_blank"
                  rel="noreferrer"
                >
                  Watch {formatDuration(r.duration_s)}
                </a>
              ) : (
                <span className="text-vmd-textSubtle">{r.status === 'RECORDING' ? 'Recording…' : r.status.toLowerCase()}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
