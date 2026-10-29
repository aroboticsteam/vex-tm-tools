import { useState } from 'react';
import { Plus, Trash2 } from 'lucide-react';
import { useDeleteOverlay, useOverlayPreview, useOverlays, useSpotlightTeam } from '../api/overlays';
import { useTeams } from '../api/teams';
import { useUiStore } from '../stores/ui';
import { Input } from '../components/ui/Input';
import { usePermission } from '../hooks/usePermission';
import { PageHeader, Card } from '../components/ui/Card';
import { Button } from '../components/ui/Button';
import { CopyButton } from '../components/ui/CopyButton';
import { OverlayEditorModal } from '../components/overlays/OverlayEditorModal';
import type { OverlayInstance } from '../types/api';

/**
 * GAP: `GET /api/v1/overlays/{entity_id}/preview` is a documented stub in
 * the merged backend (backend/routers/overlays.py's own docstring: "the
 * full team-video pipeline ... isn't wired up by this wave" — it always
 * returns `{match: null, teams: []}`). We still call the real endpoint
 * (never fabricate match/video data) and surface that limitation visibly
 * below the preview panel rather than pretending it's live.
 */
export function OverlaysPage() {
  const { data: overlays, isLoading } = useOverlays();
  const deleteOverlay = useDeleteOverlay();
  const canEdit = usePermission('overlays:edit');
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<OverlayInstance | null>(null);
  const [previewing, setPreviewing] = useState<string | null>(null);
  const { data: preview } = useOverlayPreview(previewing);

  return (
    <div>
      <PageHeader
        title="Overlays"
        subtitle="OBS browser-source instances showing upcoming-match team videos."
        action={
          canEdit && (
            <Button
              variant="primary"
              onClick={() => {
                setEditing(null);
                setModalOpen(true);
              }}
            >
              <Plus size={16} /> New Overlay
            </Button>
          )
        }
      />

      {isLoading && <p className="text-sm text-vmd-textSubtle">Loading…</p>}
      <div className="space-y-3">
        {overlays?.map((overlay) => {
          const url = `${window.location.origin}/overlay/${overlay.entity_id}`;
          return (
            <Card key={overlay.entity_id}>
              <div className="flex items-start justify-between">
                <div>
                  <p className="font-medium text-vmd-textStrong">{overlay.display_name}</p>
                  <p className="text-xs text-vmd-textSubtle">
                    {overlay.entity_id} · fieldset {overlay.field_set_id}
                  </p>
                </div>
                <div className="flex items-center gap-3">
                  <button
                    onClick={() => setPreviewing(previewing === overlay.entity_id ? null : overlay.entity_id)}
                    className="text-sm text-vmd-link hover:underline"
                  >
                    {previewing === overlay.entity_id ? 'Hide preview' : 'Preview'}
                  </button>
                  <button
                    onClick={() => {
                      setEditing(overlay);
                      setModalOpen(true);
                    }}
                    className="text-sm text-vmd-link hover:underline"
                  >
                    Edit
                  </button>
                  {canEdit && (
                    <button
                      onClick={() => deleteOverlay.mutate(overlay.entity_id)}
                      className="text-vmd-textSubtle hover:text-vmd-danger"
                    >
                      <Trash2 size={16} />
                    </button>
                  )}
                </div>
              </div>
              <div className="mt-2 flex items-center gap-2 text-xs text-vmd-textMuted">
                <span className="truncate">{url}</span>
                <CopyButton value={url} />
              </div>

              {canEdit && <SpotlightControls entityId={overlay.entity_id} />}

              {previewing === overlay.entity_id && (
                <div className="mt-3 rounded-lg border border-vmd-border bg-black p-4">
                  {preview?.teams && preview.teams.length > 0 ? (
                    <div className="grid grid-cols-2 gap-2">
                      {preview.teams.map((team) => (
                        <div key={team} className="rounded border border-vmd-border p-2 text-center text-sm">
                          {team}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="text-center text-sm text-vmd-textSubtle">No match currently queued (transparent).</p>
                  )}
                  <p className="mt-2 text-xs text-vmd-textSubtle">
                    Note: the backend's <code>/preview</code> endpoint is a documented stub as of this wave
                    — it always reports no queued match, regardless of TM state.
                  </p>
                </div>
              )}
            </Card>
          );
        })}
        {overlays && overlays.length === 0 && <p className="text-sm text-vmd-textSubtle">No overlay instances yet.</p>}
      </div>

      <OverlayEditorModal open={modalOpen} onClose={() => setModalOpen(false)} editing={editing} />
    </div>
  );
}

/** Finals "team spotlight" operator control: pick a team to put on the
 * overlay (robot 360 + info + team clip), or return to the match view. */
function SpotlightControls({ entityId }: { entityId: string }) {
  const { data: teams = [] } = useTeams();
  const spotlight = useSpotlightTeam();
  const pushToast = useUiStore((s) => s.pushToast);
  const [team, setTeam] = useState('');
  const listId = `spotlight-teams-${entityId}`;
  const send = (value: string | null) =>
    spotlight.mutate(
      { entityId, team: value },
      {
        onSuccess: () => pushToast(value ? `Spotlighting ${value}` : 'Back to the match view', 'success'),
        onError: (err) => pushToast(err instanceof Error ? err.message : 'Request failed', 'error'),
      },
    );
  const clean = team.trim().toUpperCase();
  return (
    <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-vmd-border pt-3">
      <span className="text-xs font-semibold uppercase tracking-wide text-vmd-textMuted">Finals spotlight</span>
      <Input
        className="max-w-[10rem]"
        placeholder="Team, e.g. 1234A"
        aria-label={`Spotlight team for ${entityId}`}
        list={listId}
        value={team}
        onChange={(e) => setTeam(e.target.value)}
      />
      <datalist id={listId}>
        {teams.map((t) => (
          <option key={t.team_number} value={t.team_number} />
        ))}
      </datalist>
      <Button variant="primary" disabled={!clean || spotlight.isPending} onClick={() => send(clean)}>
        Show team
      </Button>
      <Button variant="secondary" disabled={spotlight.isPending} onClick={() => send(null)}>
        Back to match view
      </Button>
    </div>
  );
}
