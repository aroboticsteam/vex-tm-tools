import { useEffect, useState } from 'react';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button';
import { Input, Label, Textarea } from '../ui/Input';
import { YamlEditor } from '../ui/YamlEditor';
import { ActionChainBuilder } from './ActionChainBuilder';
import { useCreateScript, useUpdateScript } from '../../api/scripts';
import { useUiStore } from '../../stores/ui';
import type { Script } from '../../types/api';

export function ScriptEditorModal({
  open,
  onClose,
  editing,
}: {
  open: boolean;
  onClose: () => void;
  editing: Script | null;
}) {
  const createScript = useCreateScript();
  const updateScript = useUpdateScript();
  const pushToast = useUiStore((s) => s.pushToast);

  const [tab, setTab] = useState<'builder' | 'yaml'>(editing ? 'yaml' : 'builder');
  const [name, setName] = useState(editing?.name ?? '');
  const [description, setDescription] = useState(editing?.description ?? '');
  const [actionYaml, setActionYaml] = useState(editing?.action_yaml ?? '');

  useEffect(() => {
    setTab(editing ? 'yaml' : 'builder');
    setName(editing?.name ?? '');
    setDescription(editing?.description ?? '');
    setActionYaml(editing?.action_yaml ?? '');
  }, [editing, open]);

  function handleSave() {
    if (!name || !actionYaml) {
      pushToast('Name and action chain are required.', 'error');
      return;
    }
    const body = { name, description: description || null, action_yaml: actionYaml };
    const mutation = editing ? updateScript.mutateAsync({ id: editing.id, body }) : createScript.mutateAsync(body);
    mutation
      .then(() => {
        pushToast('Saved', 'success');
        onClose();
      })
      .catch((err: unknown) => pushToast(err instanceof Error ? err.message : 'Save failed', 'error'));
  }

  return (
    <Modal open={open} onClose={onClose} title={editing ? `Edit: ${editing.name}` : 'New Script'} wide>
      <div className="space-y-3">
        <div>
          <Label>Name</Label>
          <Input value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div>
          <Label>Description</Label>
          <Textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
        </div>

        <div className="flex overflow-x-auto gap-2 border-b border-vmd-border">
          {(['builder', 'yaml'] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-3 py-1.5 text-sm ${tab === t ? 'border-b-2 border-vmd-textStrong text-vmd-textStrong' : 'text-vmd-textMuted'}`}
            >
              {t === 'builder' ? 'Builder' : 'Raw YAML'}
            </button>
          ))}
        </div>

        {tab === 'builder' ? (
          <ActionChainBuilder onYamlChange={setActionYaml} />
        ) : (
          <YamlEditor value={actionYaml} onChange={setActionYaml} rows={8} />
        )}

        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" onClick={handleSave} disabled={createScript.isPending || updateScript.isPending}>
            Save
          </Button>
        </div>
      </div>
    </Modal>
  );
}
