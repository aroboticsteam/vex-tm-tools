import { useState } from 'react';
import { Trash2, Upload } from 'lucide-react';
import {
  useDeleteMatchRecording,
  useImportMatchRecordings,
  useMatchRecordings,
  useSetMatchRecording,
  type RecordingImportResult,
} from '../../api/site';
import { isHttpUrl } from '../../lib/siteConfig';
import { useUiStore } from '../../stores/ui';
import { Button } from '../ui/Button';
import { Card } from '../ui/Card';
import { Input, Label } from '../ui/Input';
import { Modal } from '../ui/Modal';

/** Links from finished matches to their recordings, shown as “▶ Watch” on
 * the public site. One at a time, or pasted in bulk. */
export function MatchRecordingsCard({ canEdit }: { canEdit: boolean }) {
  const { data: items = [] } = useMatchRecordings();
  const save = useSetMatchRecording();
  const del = useDeleteMatchRecording();
  const pushToast = useUiStore((s) => s.pushToast);
  const [division, setDivision] = useState('1');
  const [label, setLabel] = useState('');
  const [url, setUrl] = useState('');
  const [importing, setImporting] = useState(false);
  const toastErr = (err: unknown) => pushToast(err instanceof Error ? err.message : 'Request failed', 'error');
  const valid = division.trim() !== '' && label.trim() !== '' && isHttpUrl(url);

  return (
    <Card className="lg:col-span-2">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-vmd-textMuted">Match recordings</h2>
        {canEdit && (
          <Button variant="secondary" onClick={() => setImporting(true)}>
            <Upload size={14} /> Bulk import
          </Button>
        )}
      </div>
      {canEdit && (
        <form
          className="mb-3 grid items-end gap-2 md:grid-cols-[6rem_8rem_1fr_auto]"
          onSubmit={(e) => {
            e.preventDefault();
            if (!valid) return;
            save.mutate(
              { division: division.trim(), label: label.trim(), url: url.trim() },
              { onSuccess: () => { setLabel(''); setUrl(''); pushToast('Recording linked', 'success'); }, onError: toastErr },
            );
          }}
        >
          <div>
            <Label>Division</Label>
            <Input value={division} onChange={(e) => setDivision(e.target.value)} aria-label="Recording division" />
          </div>
          <div>
            <Label>Match (e.g. Q12)</Label>
            <Input value={label} onChange={(e) => setLabel(e.target.value)} aria-label="Recording match" />
          </div>
          <div>
            <Label>Recording link</Label>
            <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://youtu.be/…?t=95" aria-label="Recording link" />
          </div>
          <Button variant="primary" type="submit" disabled={!valid || save.isPending}>
            Link
          </Button>
        </form>
      )}
      {items.length === 0 && <p className="text-sm text-vmd-textSubtle">No recordings linked yet.</p>}
      {items.map((r) => (
        <div key={r.match_key} data-testid="recording-row" className="flex items-center justify-between gap-2 border-b border-vmd-border py-1.5 text-sm last:border-0">
          <span>
            <b className="mr-2 text-vmd-textStrong">Div {r.division} · {r.label}</b>
            <a href={r.url} target="_blank" rel="noreferrer" className="text-vmd-link hover:underline">
              {r.title || r.url}
            </a>
          </span>
          {canEdit && (
            <Button variant="ghost" aria-label={`Remove recording ${r.label}`} onClick={() => del.mutate(r.match_key, { onError: toastErr })}>
              <Trash2 size={14} />
            </Button>
          )}
        </div>
      ))}
      <ImportModal open={importing} onClose={() => setImporting(false)} key={importing ? 'open' : 'closed'} />
    </Card>
  );
}

function ImportModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const run = useImportMatchRecordings();
  const pushToast = useUiStore((s) => s.pushToast);
  const [text, setText] = useState('');
  const [result, setResult] = useState<RecordingImportResult | null>(null);
  return (
    <Modal open={open} onClose={onClose} title="Import recording links">
      <div className="space-y-3">
        <p className="text-xs text-vmd-textSubtle">
          One per line: <code>division match link [title]</code>, e.g. <code>1 Q12 https://youtu.be/abc?t=95 Qualification 12</code>. All-or-nothing.
        </p>
        <textarea
          className="vmd-code-block h-40 w-full rounded-lg border border-vmd-border bg-vmd-elevated p-2 font-mono text-xs"
          aria-label="Recording lines"
          value={text}
          onChange={(e) => { setText(e.target.value); setResult(null); }}
          spellCheck={false}
        />
        {result && result.errors.length > 0 && (
          <ul className="text-xs text-vmd-danger" data-testid="recording-import-errors">
            {result.errors.map((e) => (
              <li key={`${e.line}-${e.error}`}>Line {e.line}: {e.error}</li>
            ))}
          </ul>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            disabled={!text.trim() || run.isPending}
            onClick={() =>
              run.mutate(text, {
                onSuccess: (r) => {
                  setResult(r);
                  if (!r.errors.length) { pushToast(`Linked ${r.saved} recording${r.saved === 1 ? '' : 's'}`, 'success'); onClose(); }
                },
                onError: (err) => pushToast(err instanceof Error ? err.message : 'Import failed', 'error'),
              })
            }
          >
            Import
          </Button>
        </div>
      </div>
    </Modal>
  );
}
