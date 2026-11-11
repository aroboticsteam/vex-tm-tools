import { useEffect, useState } from 'react';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button';
import { Input, Label } from '../ui/Input';
import { TagsEditor } from '../integrations/TagsEditor';
import { useCreateTimer, useUpdateTimer } from '../../api/timers';
import { useUiStore } from '../../stores/ui';
import type { TimerInstance } from '../../types/api';

export function TimerEditorModal({
  open,
  onClose,
  editing,
}: {
  open: boolean;
  onClose: () => void;
  editing: TimerInstance | null;
}) {
  const createTimer = useCreateTimer();
  const updateTimer = useUpdateTimer();
  const pushToast = useUiStore((s) => s.pushToast);

  const [entityId, setEntityId] = useState(editing?.entity_id ?? '');
  const [displayName, setDisplayName] = useState(editing?.display_name ?? '');
  const [fieldSetId, setFieldSetId] = useState(String(editing?.field_set_id ?? ''));
  const [fieldId, setFieldId] = useState(String(editing?.field_id ?? ''));
  const [durationS, setDurationS] = useState(String(editing?.duration_s ?? 120));
  const [tags, setTags] = useState<string[]>(editing?.tags ?? []);

  useEffect(() => {
    setEntityId(editing?.entity_id ?? '');
    setDisplayName(editing?.display_name ?? '');
    setFieldSetId(String(editing?.field_set_id ?? ''));
    setFieldId(String(editing?.field_id ?? ''));
    setDurationS(String(editing?.duration_s ?? 120));
    setTags(editing?.tags ?? []);
  }, [editing, open]);

  function handleSave() {
    if (!entityId || !displayName || fieldSetId === '' || fieldId === '') {
      pushToast('Entity ID, display name, field set, and field are required.', 'error');
      return;
    }
    const common = {
      display_name: displayName,
      field_set_id: Number(fieldSetId),
      field_id: Number(fieldId),
      duration_s: Number(durationS),
      tags,
    };
    const mutation = editing
      ? updateTimer.mutateAsync({ entityId: editing.entity_id, body: common })
      : createTimer.mutateAsync({ entity_id: entityId, ...common });
    mutation
      .then(() => {
        pushToast('Saved', 'success');
        onClose();
      })
      .catch((err: unknown) => pushToast(err instanceof Error ? err.message : 'Save failed', 'error'));
  }

  return (
    <Modal open={open} onClose={onClose} title={editing ? `Edit: ${editing.display_name}` : 'New Timer Instance'}>
      <div className="space-y-3">
        <div>
          <Label>Entity ID</Label>
          <Input
            value={entityId}
            onChange={(e) => setEntityId(e.target.value)}
            disabled={Boolean(editing)}
            placeholder="timer.fs1_field1"
          />
        </div>
        <div>
          <Label>Display Name</Label>
          <Input value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
          <div>
            <Label>Field Set ID</Label>
            <Input type="number" value={fieldSetId} onChange={(e) => setFieldSetId(e.target.value)} />
          </div>
          <div>
            <Label>Field ID</Label>
            <Input type="number" value={fieldId} onChange={(e) => setFieldId(e.target.value)} />
          </div>
        </div>
        <div>
          <Label>Countdown duration (seconds)</Label>
          <Input type="number" value={durationS} onChange={(e) => setDurationS(e.target.value)} />
        </div>
        <div>
          <Label>Tags</Label>
          <TagsEditor tags={tags} onChange={setTags} />
        </div>
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" onClick={handleSave} disabled={createTimer.isPending || updateTimer.isPending}>
            Save
          </Button>
        </div>
      </div>
    </Modal>
  );
}
