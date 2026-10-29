import { useState } from 'react';
import { Plus, Trash2, Zap } from 'lucide-react';
import {
  useZerosPresets,
  useCreateZerosPreset,
  useDeleteZerosPreset,
  useCallIntegrationService,
} from '../../api/integrations';
import { usePermission } from '../../hooks/usePermission';
import { useUiStore } from '../../stores/ui';
import { Button } from '../ui/Button';
import { Input, Label } from '../ui/Input';
import type { IntegrationInstance } from '../../types/api';

/** Plan §11's ZerOS presets CRUD (`GET/POST/PUT/DELETE /api/v1/zeros/presets`)
 * had a complete backend + a complete set of TanStack Query hooks
 * (`useZerosPresets`/`useCreateZerosPreset`/`useDeleteZerosPreset` in
 * `api/integrations.ts`) but no page or component anywhere called them — the
 * only way to manage a preset was a raw API request. This renders them per
 * ZerOS integration instance on the Integrations page, mirroring
 * `SpotifyAuthButton`'s per-domain-section pattern. */
export function ZerosPresetsSection({ integration }: { integration: IntegrationInstance }) {
  const { data: presets, isLoading } = useZerosPresets();
  const createPreset = useCreateZerosPreset();
  const deletePreset = useDeleteZerosPreset();
  const callService = useCallIntegrationService();
  const canFire = usePermission('vfx:control');
  const pushToast = useUiStore((s) => s.pushToast);

  const [presetNumber, setPresetNumber] = useState('');
  const [presetName, setPresetName] = useState('');

  const instancePresets = (presets ?? [])
    // `integration_id` is the instance's UUID (`id`), not its `entity_id` slug.
    .filter((p) => p.integration_id === integration.id)
    .sort((a, b) => a.preset_number - b.preset_number);

  function handleCreate() {
    const number = Number(presetNumber);
    if (!Number.isFinite(number) || !presetName.trim()) {
      pushToast('Preset number and name are required.', 'error');
      return;
    }
    createPreset.mutate(
      { integration_id: integration.id, preset_number: number, preset_name: presetName.trim() },
      {
        onSuccess: () => {
          setPresetNumber('');
          setPresetName('');
          pushToast('Preset created', 'success');
        },
        onError: (err) => pushToast(err instanceof Error ? err.message : 'Failed to create preset', 'error'),
      },
    );
  }

  /** Fires the preset on the board now (`set_preset` service -> OSC
   * `/zeros/playback/go/<number>`), same call an automation action makes. */
  function handleFire(presetNumber: number, name: string) {
    callService.mutate(
      { entityId: integration.entity_id, service: 'set_preset', data: { preset_id: presetNumber } },
      {
        onSuccess: () => pushToast(`Fired preset #${presetNumber} ${name}`, 'success'),
        onError: (err) => pushToast(err instanceof Error ? err.message : 'Failed to fire preset', 'error'),
      },
    );
  }

  function handleDelete(id: string) {
    deletePreset.mutate(id, {
      onError: (err) => pushToast(err instanceof Error ? err.message : 'Failed to delete preset', 'error'),
    });
  }

  return (
    <div className="mt-3 border-t border-vmd-border pt-3">
      <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-vmd-textMuted">ZerOS Presets</p>

      {isLoading ? (
        <p className="text-sm text-vmd-textSubtle">Loading…</p>
      ) : instancePresets.length === 0 ? (
        <p className="text-sm text-vmd-textSubtle">No presets yet.</p>
      ) : (
        <ul className="mb-2 space-y-1">
          {instancePresets.map((preset) => (
            <li key={preset.id} className="flex items-center justify-between text-sm">
              <span>
                <span className="text-vmd-textSubtle">#{preset.preset_number}</span> {preset.preset_name}
              </span>
              <span className="flex items-center gap-2">
                <Button
                  variant="secondary"
                  className="!px-2.5 !py-1 text-xs"
                  onClick={() => handleFire(preset.preset_number, preset.preset_name)}
                  disabled={!canFire || callService.isPending}
                  title={canFire ? 'Fire this preset now' : 'Requires the vfx:control permission'}
                >
                  <Zap size={12} /> Fire
                </Button>
                <button
                  onClick={() => handleDelete(preset.id)}
                  className="text-vmd-textSubtle hover:text-vmd-danger"
                  title="Delete preset"
                >
                  <Trash2 size={14} />
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}

      <div className="flex items-end gap-2">
        <div className="w-20">
          <Label>#</Label>
          <Input
            type="number"
            value={presetNumber}
            onChange={(e) => setPresetNumber(e.target.value)}
          />
        </div>
        <div className="flex-1">
          <Label>Name</Label>
          <Input value={presetName} onChange={(e) => setPresetName(e.target.value)} />
        </div>
        <Button variant="secondary" onClick={handleCreate} disabled={createPreset.isPending}>
          <Plus size={14} /> Add
        </Button>
      </div>
    </div>
  );
}
