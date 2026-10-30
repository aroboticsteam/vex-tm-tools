import { useEffect, useRef, useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { useDeleteTeamLogo, useTeam, useUpdateTeam, useUploadTeamLogo } from '../api/teams';
import { useUiStore } from '../stores/ui';
import { usePermission } from '../hooks/usePermission';
import { PageHeader, Card } from '../components/ui/Card';
import { Button } from '../components/ui/Button';
import { Input, Label, Textarea } from '../components/ui/Input';
import { VideoUploader } from '../components/teams/VideoUploader';
import { ChecklistCard } from '../components/teams/ChecklistCard';
import { TeamAlertCard } from '../components/teams/TeamAlertCard';

export function TeamDetailPage() {
  const { number } = useParams<{ number: string }>();
  const { data: team } = useTeam(number ?? null);
  const updateTeam = useUpdateTeam();
  const canEdit = usePermission('teams:edit');
  const canAlert = usePermission('notifications:broadcast');

  const [pitLocation, setPitLocation] = useState('');
  const [extraNotes, setExtraNotes] = useState('');
  const [websiteUrl, setWebsiteUrl] = useState('');
  const uploadLogo = useUploadTeamLogo();
  const deleteLogo = useDeleteTeamLogo();
  const pushToast = useUiStore((s) => s.pushToast);
  const logoInput = useRef<HTMLInputElement>(null);
  const websiteOk = websiteUrl.trim() === '' || /^https?:\/\/\S+$/i.test(websiteUrl.trim());

  useEffect(() => {
    setPitLocation(team?.pit_location ?? '');
    setExtraNotes(team?.extra_notes ?? '');
    setWebsiteUrl(team?.website_url ?? '');
  }, [team]);

  if (!number) return null;

  const stats = team?.cached_stats ?? {};

  return (
    <div>
      <Link to="/teams" className="mb-4 inline-flex items-center gap-1 text-sm text-vmd-link hover:underline">
        <ArrowLeft size={14} /> Back to Teams
      </Link>
      <PageHeader title={`Team ${number}`} subtitle={team?.robot_name ?? undefined} />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">Profile</h2>
          <div className="space-y-3">
            <div>
              <Label>Pit Location</Label>
              <Input value={pitLocation} onChange={(e) => setPitLocation(e.target.value)} disabled={!canEdit} />
            </div>
            <div>
              <Label>Extra Notes</Label>
              <Textarea rows={3} value={extraNotes} onChange={(e) => setExtraNotes(e.target.value)} disabled={!canEdit} />
            </div>
            <div>
              <Label>Website (shown on the public team page)</Label>
              <Input
                value={websiteUrl}
                placeholder="https://team.example.com"
                onChange={(e) => setWebsiteUrl(e.target.value)}
                disabled={!canEdit}
                aria-label="Team website"
              />
              {!websiteOk && <p className="mt-1 text-xs text-vmd-danger">Must start with http:// or https://</p>}
            </div>
            <div>
              <Label>Logo (public team page)</Label>
              <div className="flex items-center gap-2">
                <span className="text-sm text-vmd-textMuted">{team?.logo_s3_key ? 'Logo uploaded' : 'No logo'}</span>
                {canEdit && (
                  <>
                    <input
                      ref={logoInput}
                      type="file"
                      accept="image/png,image/jpeg,image/gif,image/webp"
                      className="hidden"
                      aria-label="Upload team logo"
                      onChange={(e) => {
                        const file = e.target.files?.[0];
                        e.target.value = '';
                        if (!file) return;
                        uploadLogo.mutate(
                          { number, file },
                          {
                            onSuccess: () => pushToast('Logo uploaded', 'success'),
                            onError: (err) => pushToast(err instanceof Error ? err.message : 'Upload failed', 'error'),
                          },
                        );
                      }}
                    />
                    <Button variant="secondary" type="button" disabled={uploadLogo.isPending} onClick={() => logoInput.current?.click()}>
                      {team?.logo_s3_key ? 'Replace logo' : 'Upload logo'}
                    </Button>
                    {team?.logo_s3_key && (
                      <Button variant="ghost" type="button" disabled={deleteLogo.isPending} onClick={() => deleteLogo.mutate(number)}>
                        Remove
                      </Button>
                    )}
                  </>
                )}
              </div>
            </div>
            {canEdit && (
              <Button
                variant="primary"
                onClick={() =>
                  updateTeam.mutate({
                    number,
                    body: { pit_location: pitLocation, extra_notes: extraNotes, website_url: websiteUrl.trim() },
                  })
                }
                disabled={updateTeam.isPending || !websiteOk}
              >
                Save
              </Button>
            )}
          </div>

          <h2 className="mb-2 mt-5 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">
            Stats (from scraper cache)
          </h2>
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

        <ChecklistCard teamNumber={number} />

        <Card>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">
            Robot 360 Video
          </h2>
          <VideoUploader teamNumber={number} />
        </Card>
      </div>
      {canAlert && <TeamAlertCard key={number} initialTeams={number} />}
    </div>
  );
}
