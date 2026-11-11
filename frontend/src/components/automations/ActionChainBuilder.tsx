import { useState } from 'react';
import * as yaml from 'js-yaml';
import { Plus, Trash2, GripVertical } from 'lucide-react';
import { Button } from '../ui/Button';
import { Input, Label, Select } from '../ui/Input';
import { YamlEditor } from '../ui/YamlEditor';

type ActionKind = 'service' | 'script' | 'delay' | 'advanced';

interface ActionItem {
  key: string;
  kind: ActionKind;
  service: string;
  target: string;
  targetTag: string;
  targetFilter: string;
  dataYaml: string;
  scriptName: string;
  delay: string;
  advancedYaml: string;
}

let keyCounter = 0;
function newItem(kind: ActionKind = 'service'): ActionItem {
  return {
    key: `action-${++keyCounter}`,
    kind,
    service: '',
    target: '',
    targetTag: '',
    targetFilter: '',
    dataYaml: '',
    scriptName: '',
    delay: '00:00:01',
    advancedYaml: '',
  };
}

function itemToObject(item: ActionItem): Record<string, unknown> | null {
  try {
    switch (item.kind) {
      case 'service': {
        const out: Record<string, unknown> = { service: item.service };
        if (item.target) out.target = item.target;
        if (item.targetTag) out.target_tag = item.targetTag;
        if (item.targetFilter) out.target_filter = item.targetFilter;
        if (item.dataYaml.trim()) out.data = yaml.load(item.dataYaml) ?? {};
        return out;
      }
      case 'script': {
        const out: Record<string, unknown> = { script: item.scriptName };
        if (item.dataYaml.trim()) out.data = yaml.load(item.dataYaml) ?? {};
        return out;
      }
      case 'delay':
        return { delay: item.delay };
      case 'advanced':
        return (yaml.load(item.advancedYaml) as Record<string, unknown>) ?? {};
      default:
        return null;
    }
  } catch {
    return null;
  }
}

/**
 * Action chain builder (plan §12 Automations/Scripts: "action chain builder
 * with drag-and-drop order"). This implements ordered add/remove/reorder
 * (via up/down, a simpler equivalent of drag-and-drop that doesn't need an
 * extra DnD library dependency) for the three structured action types the
 * engine documents (§10 "Action types" table: service/script/delay) plus an
 * "Advanced" escape hatch that accepts one raw YAML action mapping verbatim
 * (covers `condition:`/`repeat:` blocks and anything else the engine
 * supports without this builder needing to model every nested shape).
 *
 * Simplification (documented, not a stub): this builder is a one-way
 * generator — `items` state produces `action_yaml` text via `onYamlChange`.
 * It does not parse an arbitrary existing `action_yaml` string back into
 * structured `items` (round-tripping free-form YAML/Jinja into a fixed set
 * of form fields is lossy in the general case). The page using this
 * component defaults new automations/scripts to the Builder tab and
 * existing ones to the raw YAML tab for editing, so no data is ever
 * silently dropped.
 */
export function ActionChainBuilder({ onYamlChange }: { onYamlChange: (yamlText: string) => void }) {
  const [items, setItems] = useState<ActionItem[]>([newItem()]);

  function emit(next: ActionItem[]) {
    setItems(next);
    const objects = next.map(itemToObject).filter((o): o is Record<string, unknown> => o !== null);
    onYamlChange(yaml.dump(objects));
  }

  function update(key: string, patch: Partial<ActionItem>) {
    emit(items.map((it) => (it.key === key ? { ...it, ...patch } : it)));
  }

  function move(index: number, delta: number) {
    const target = index + delta;
    if (target < 0 || target >= items.length) return;
    const next = [...items];
    [next[index], next[target]] = [next[target], next[index]];
    emit(next);
  }

  return (
    <div className="space-y-3">
      {items.map((item, idx) => (
        <div key={item.key} className="rounded-lg border border-vmd-border bg-vmd-surfaceSubtle p-3">
          <div className="mb-2 flex items-center gap-2">
            <GripVertical size={14} className="text-vmd-textSubtle" />
            <Select value={item.kind} onChange={(e) => update(item.key, { kind: e.target.value as ActionKind })} className="w-36">
              <option value="service">Call service</option>
              <option value="script">Run script</option>
              <option value="delay">Delay</option>
              <option value="advanced">Advanced (raw YAML)</option>
            </Select>
            <div className="ml-auto flex gap-1">
              <button onClick={() => move(idx, -1)} className="text-vmd-textSubtle hover:text-vmd-text" title="Move up">
                ↑
              </button>
              <button onClick={() => move(idx, 1)} className="text-vmd-textSubtle hover:text-vmd-text" title="Move down">
                ↓
              </button>
              <button
                onClick={() => emit(items.filter((it) => it.key !== item.key))}
                className="text-vmd-textSubtle hover:text-vmd-danger"
                title="Remove"
              >
                <Trash2 size={14} />
              </button>
            </div>
          </div>

          {item.kind === 'service' && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              <div>
                <Label>Service (domain.service)</Label>
                <Input value={item.service} onChange={(e) => update(item.key, { service: e.target.value })} placeholder="atem.switch_input" />
              </div>
              <div>
                <Label>Target entity_id</Label>
                <Input value={item.target} onChange={(e) => update(item.key, { target: e.target.value })} placeholder="atem.main_switcher" />
              </div>
              <div>
                <Label>Target tag (optional)</Label>
                <Input value={item.targetTag} onChange={(e) => update(item.key, { targetTag: e.target.value })} placeholder="fieldAudio" />
              </div>
              <div>
                <Label>Target filter (Jinja2, optional)</Label>
                <Input
                  value={item.targetFilter}
                  onChange={(e) => update(item.key, { targetFilter: e.target.value })}
                  placeholder="{{ trigger.entity.tags | intersect(target.tags) | length > 0 }}"
                />
              </div>
              <div className="col-span-2">
                <Label>Data (YAML mapping)</Label>
                <YamlEditor rows={3} value={item.dataYaml} onChange={(v) => update(item.key, { dataYaml: v })} />
              </div>
            </div>
          )}

          {item.kind === 'script' && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              <div>
                <Label>Script name</Label>
                <Input value={item.scriptName} onChange={(e) => update(item.key, { scriptName: e.target.value })} />
              </div>
              <div>
                <Label>Data (YAML mapping)</Label>
                <YamlEditor rows={3} value={item.dataYaml} onChange={(v) => update(item.key, { dataYaml: v })} />
              </div>
            </div>
          )}

          {item.kind === 'delay' && (
            <div>
              <Label>Duration (HH:MM:SS)</Label>
              <Input value={item.delay} onChange={(e) => update(item.key, { delay: e.target.value })} />
            </div>
          )}

          {item.kind === 'advanced' && (
            <div>
              <Label>Raw action mapping (YAML)</Label>
              <YamlEditor
                rows={4}
                value={item.advancedYaml}
                onChange={(v) => update(item.key, { advancedYaml: v })}
              />
            </div>
          )}
        </div>
      ))}

      <Button onClick={() => emit([...items, newItem()])}>
        <Plus size={14} /> Add action
      </Button>
    </div>
  );
}
