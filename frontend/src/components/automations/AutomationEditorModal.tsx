import { useEffect, useState } from 'react';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button';
import { Input, Label, Select } from '../ui/Input';
import { YamlEditor } from '../ui/YamlEditor';
import { TriggerBuilder } from './TriggerBuilder';
import { ConditionBuilder } from './ConditionBuilder';
import { ActionChainBuilder } from './ActionChainBuilder';
import {
  useAutomationFolders,
  useCreateAutomation,
  useUpdateAutomation,
  useValidateAutomation,
} from '../../api/automations';
import { useUiStore } from '../../stores/ui';
import type { Automation } from '../../types/api';

/**
 * Plan §12 Automations editor: form builder + raw YAML editor tab, plus
 * the Appendix A.7 "Validate" button. Per the note in
 * `ActionChainBuilder`'s docstring, editing an existing automation
 * defaults to the YAML tab (builder state can't be reconstructed from
 * arbitrary stored YAML) while creating a new one defaults to Builder.
 */
export function AutomationEditorModal({
  open,
  onClose,
  editing,
}: {
  open: boolean;
  onClose: () => void;
  editing: Automation | null;
}) {
  const { data: folders } = useAutomationFolders();
  const createAutomation = useCreateAutomation();
  const updateAutomation = useUpdateAutomation();
  const validate = useValidateAutomation();
  const pushToast = useUiStore((s) => s.pushToast);

  const [tab, setTab] = useState<'builder' | 'yaml'>(editing ? 'yaml' : 'builder');
  const [alias, setAlias] = useState(editing?.alias ?? '');
  const [folderId, setFolderId] = useState(editing?.folder_id ?? '');
  const [triggerYaml, setTriggerYaml] = useState(editing?.trigger_yaml ?? '');
  const [conditionYaml, setConditionYaml] = useState(editing?.condition_yaml ?? '');
  const [actionYaml, setActionYaml] = useState(editing?.action_yaml ?? '');
  const [validation, setValidation] = useState<{ valid: boolean; errors: string[] } | null>(null);

  useEffect(() => {
    setTab(editing ? 'yaml' : 'builder');
    setAlias(editing?.alias ?? '');
    setFolderId(editing?.folder_id ?? '');
    setTriggerYaml(editing?.trigger_yaml ?? '');
    setConditionYaml(editing?.condition_yaml ?? '');
    setActionYaml(editing?.action_yaml ?? '');
    setValidation(null);
  }, [editing, open]);

  function handleValidate() {
    validate.mutate(
      { trigger_yaml: triggerYaml, condition_yaml: conditionYaml || null, action_yaml: actionYaml },
      { onSuccess: setValidation },
    );
  }

  function handleSave() {
    if (!alias || !triggerYaml || !actionYaml) {
      pushToast('Alias, trigger, and action are required.', 'error');
      return;
    }
    const body = {
      alias,
      folder_id: folderId || null,
      trigger_yaml: triggerYaml,
      condition_yaml: conditionYaml || null,
      action_yaml: actionYaml,
    };
    const mutation = editing
      ? updateAutomation.mutateAsync({ id: editing.id, body })
      : createAutomation.mutateAsync(body);
    mutation
      .then(() => {
        pushToast('Saved', 'success');
        onClose();
      })
      .catch((err: unknown) => pushToast(err instanceof Error ? err.message : 'Save failed', 'error'));
  }

  return (
    <Modal open={open} onClose={onClose} title={editing ? `Edit: ${editing.alias}` : 'New Automation'} wide>
      <div className="space-y-3">
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
          <div>
            <Label>Alias</Label>
            <Input value={alias} onChange={(e) => setAlias(e.target.value)} />
          </div>
          <div>
            <Label>Folder</Label>
            <Select value={folderId} onChange={(e) => setFolderId(e.target.value)}>
              <option value="">(none)</option>
              {folders?.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.name}
                </option>
              ))}
            </Select>
          </div>
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
          <div className="space-y-4">
            <div>
              <p className="mb-1 text-xs font-semibold uppercase text-vmd-textSubtle">Trigger</p>
              <TriggerBuilder onYamlChange={setTriggerYaml} />
            </div>
            <div>
              <p className="mb-1 text-xs font-semibold uppercase text-vmd-textSubtle">Condition</p>
              <ConditionBuilder onYamlChange={setConditionYaml} />
            </div>
            <div>
              <p className="mb-1 text-xs font-semibold uppercase text-vmd-textSubtle">Action chain</p>
              <ActionChainBuilder onYamlChange={setActionYaml} />
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            <div>
              <Label>trigger_yaml</Label>
              <YamlEditor value={triggerYaml} onChange={setTriggerYaml} rows={4} />
            </div>
            <div>
              <Label>condition_yaml</Label>
              <YamlEditor value={conditionYaml} onChange={setConditionYaml} rows={3} />
            </div>
            <div>
              <Label>action_yaml</Label>
              <YamlEditor value={actionYaml} onChange={setActionYaml} rows={6} />
            </div>
          </div>
        )}

        {validation && (
          <div className={validation.valid ? 'text-sm text-vmd-success' : 'text-sm text-vmd-danger'}>
            {validation.valid ? 'Valid.' : validation.errors.join('; ')}
          </div>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={handleValidate} disabled={validate.isPending}>
            Validate
          </Button>
          <Button
            variant="primary"
            onClick={handleSave}
            disabled={createAutomation.isPending || updateAutomation.isPending}
          >
            Save
          </Button>
        </div>
      </div>
    </Modal>
  );
}
