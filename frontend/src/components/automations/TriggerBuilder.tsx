import { useState } from 'react';
import * as yaml from 'js-yaml';
import { Plus, Trash2 } from 'lucide-react';
import { Button } from '../ui/Button';
import { Input, Label, Select } from '../ui/Input';

interface TriggerItem {
  key: string;
  platform: 'state' | 'timer_milestone';
  entityId: string;
  tag: string;
  eventType: string;
  remaining: string;
}

let counter = 0;
function newTrigger(): TriggerItem {
  return { key: `trig-${++counter}`, platform: 'state', entityId: '', tag: '', eventType: '', remaining: '00:00:10' };
}

function toObject(t: TriggerItem): Record<string, unknown> {
  const out: Record<string, unknown> = { platform: t.platform };
  if (t.entityId) out.entity_id = t.entityId;
  if (t.tag) out.tag = t.tag;
  if (t.platform === 'state' && t.eventType) out.event_type = t.eventType;
  if (t.platform === 'timer_milestone') out.remaining = t.remaining;
  return out;
}

/** Plan §12 Automations "trigger picker". Supports the two trigger
 * platforms the engine/plan document (`state` — an integration event type
 * on an entity_id, and `timer_milestone` — a tag/entity_id + remaining
 * time), each targetable by `entity_id` or `tag`. */
export function TriggerBuilder({ onYamlChange }: { onYamlChange: (yamlText: string) => void }) {
  const [items, setItems] = useState<TriggerItem[]>([newTrigger()]);

  function emit(next: TriggerItem[]) {
    setItems(next);
    onYamlChange(yaml.dump(next.map(toObject)));
  }

  function update(key: string, patch: Partial<TriggerItem>) {
    emit(items.map((it) => (it.key === key ? { ...it, ...patch } : it)));
  }

  return (
    <div className="space-y-2">
      {items.map((item) => (
        <div key={item.key} className="rounded-lg border border-vmd-border bg-vmd-surfaceSubtle p-3">
          <div className="mb-2 flex items-center justify-between">
            <Select value={item.platform} onChange={(e) => update(item.key, { platform: e.target.value as TriggerItem['platform'] })} className="w-40">
              <option value="state">State / event</option>
              <option value="timer_milestone">Timer milestone</option>
            </Select>
            <button onClick={() => emit(items.filter((it) => it.key !== item.key))} className="text-vmd-textSubtle hover:text-vmd-danger">
              <Trash2 size={14} />
            </button>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            <div>
              <Label>Entity ID</Label>
              <Input value={item.entityId} onChange={(e) => update(item.key, { entityId: e.target.value })} placeholder="vex_tm.division_1" />
            </div>
            <div>
              <Label>Tag (alternative to entity_id)</Label>
              <Input value={item.tag} onChange={(e) => update(item.key, { tag: e.target.value })} placeholder="fieldCountdown" />
            </div>
            {item.platform === 'state' ? (
              <div className="col-span-2">
                <Label>Event type</Label>
                <Input value={item.eventType} onChange={(e) => update(item.key, { eventType: e.target.value })} placeholder="matchStarted" />
              </div>
            ) : (
              <div className="col-span-2">
                <Label>Remaining (HH:MM:SS)</Label>
                <Input value={item.remaining} onChange={(e) => update(item.key, { remaining: e.target.value })} />
              </div>
            )}
          </div>
        </div>
      ))}
      <Button onClick={() => emit([...items, newTrigger()])}>
        <Plus size={14} /> Add trigger
      </Button>
    </div>
  );
}
