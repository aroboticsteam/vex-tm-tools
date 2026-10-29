import { useMemo, useState } from 'react';
import { ArrowDown, ArrowUp, Plus, Trash2 } from 'lucide-react';
import { useIntegrationSchemas, useIntegrations } from '../../api/integrations';
import { useOverlays } from '../../api/overlays';
import { useScripts } from '../../api/scripts';
import {
  HTTP_METHODS,
  chainProblems,
  coerceFieldValue,
  moveItem,
  newStep,
  stepsToYaml,
  yamlToSteps,
  type Step,
  type StepKind,
} from '../../lib/actionBuilder';
import { Button } from '../ui/Button';
import { Input, Label, Select, Textarea } from '../ui/Input';
import { YamlEditor } from '../ui/YamlEditor';

const KIND_LABELS: Record<StepKind, string> = {
  service: 'Call an integration service',
  script: 'Run a script',
  delay: 'Wait (delay)',
  spotlight: 'Spotlight a team on the overlay',
  http: 'Send an HTTP request',
};

/** Visual editor for a button's action chain, with a YAML escape hatch. The
 * two views share one `value` (the YAML text); the visual view is only
 * offered while the YAML is something it can represent without loss. */
export function ActionBuilder({ value, onChange }: { value: string; onChange: (yaml: string) => void }) {
  const initial = useMemo(() => yamlToSteps(value), []); // eslint-disable-line react-hooks/exhaustive-deps
  const [mode, setMode] = useState<'visual' | 'yaml'>(initial.ok ? 'visual' : 'yaml');
  const [steps, setSteps] = useState<Step[]>(initial.steps);
  const [modeError, setModeError] = useState<string | null>(null);

  function commit(next: Step[]) {
    setSteps(next);
    onChange(stepsToYaml(next));
  }

  function showVisual() {
    const parsed = yamlToSteps(value);
    if (!parsed.ok) {
      setModeError('This YAML uses features the visual builder cannot show (conditions, repeats, tag targets…). Keep editing it as YAML.');
      return;
    }
    setModeError(null);
    setSteps(parsed.steps);
    setMode('visual');
  }

  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <div className="flex gap-1.5" role="tablist" aria-label="Action editor mode">
          <Button variant={mode === 'visual' ? 'primary' : 'secondary'} onClick={showVisual} role="tab" aria-selected={mode === 'visual'}>
            Visual
          </Button>
          <Button
            variant={mode === 'yaml' ? 'primary' : 'secondary'}
            onClick={() => {
              setModeError(null);
              setMode('yaml');
            }}
            role="tab"
            aria-selected={mode === 'yaml'}
          >
            YAML
          </Button>
        </div>
      </div>
      {modeError && <p className="mb-2 text-xs text-vmd-danger">{modeError}</p>}
      {mode === 'yaml' ? (
        <YamlEditor value={value} onChange={onChange} rows={10} />
      ) : (
        <VisualEditor steps={steps} onChange={commit} />
      )}
    </div>
  );
}

function VisualEditor({ steps, onChange }: { steps: Step[]; onChange: (steps: Step[]) => void }) {
  const [addKind, setAddKind] = useState<StepKind>('service');
  const problems = chainProblems(steps);
  const update = (id: string, patch: Partial<Step>) =>
    onChange(steps.map((s) => (s.id === id ? ({ ...s, ...patch } as Step) : s)));

  return (
    <div className="space-y-3" data-testid="visual-editor">
      {steps.length === 0 && <p className="text-sm text-vmd-textSubtle">No steps yet — add the first one below.</p>}
      {steps.map((step, index) => (
        <div key={step.id} className="rounded-lg border border-vmd-border bg-vmd-surfaceSubtle p-3" data-testid="step">
          <div className="mb-2 flex items-center justify-between gap-2">
            <p className="text-xs font-semibold uppercase tracking-wide text-vmd-textMuted">
              {index + 1}. {KIND_LABELS[step.kind]}
            </p>
            <div className="flex gap-1">
              <Button variant="ghost" title="Move up" aria-label="Move step up" disabled={index === 0} onClick={() => onChange(moveItem(steps, index, index - 1))}>
                <ArrowUp size={14} />
              </Button>
              <Button variant="ghost" title="Move down" aria-label="Move step down" disabled={index === steps.length - 1} onClick={() => onChange(moveItem(steps, index, index + 1))}>
                <ArrowDown size={14} />
              </Button>
              <Button variant="ghost" title="Remove step" aria-label="Remove step" onClick={() => onChange(steps.filter((s) => s.id !== step.id))}>
                <Trash2 size={14} />
              </Button>
            </div>
          </div>
          <StepFields step={step} update={(patch) => update(step.id, patch)} />
        </div>
      ))}
      {problems.length > 0 && (
        <ul className="text-xs text-vmd-danger" data-testid="step-problems">
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
      <div className="flex items-center gap-2">
        <Select value={addKind} onChange={(e) => setAddKind(e.target.value as StepKind)} aria-label="Step type" className="max-w-xs">
          {(Object.keys(KIND_LABELS) as StepKind[]).map((k) => (
            <option key={k} value={k}>
              {KIND_LABELS[k]}
            </option>
          ))}
        </Select>
        <Button variant="secondary" onClick={() => onChange([...steps, newStep(addKind)])}>
          <Plus size={14} /> Add step
        </Button>
      </div>
    </div>
  );
}

function StepFields({ step, update }: { step: Step; update: (patch: Partial<Step>) => void }) {
  switch (step.kind) {
    case 'service':
      return <ServiceFields step={step} update={update} />;
    case 'script':
      return <ScriptFields step={step} update={update} />;
    case 'delay':
      return (
        <div>
          <Label>Wait (HH:MM:SS or seconds)</Label>
          <Input value={step.delay} onChange={(e) => update({ delay: e.target.value })} aria-label="Delay" />
          <p className="mt-1 text-xs text-vmd-textSubtle">Everything after this step runs once the wait is over.</p>
        </div>
      );
    case 'spotlight':
      return <SpotlightFields step={step} update={update} />;
    case 'http':
      return (
        <div className="space-y-2">
          <div className="grid grid-cols-[7rem_1fr] gap-2">
            <div>
              <Label>Method</Label>
              <Select value={step.method} onChange={(e) => update({ method: e.target.value })} aria-label="HTTP method">
                {HTTP_METHODS.map((m) => (
                  <option key={m}>{m}</option>
                ))}
              </Select>
            </div>
            <div>
              <Label>URL</Label>
              <Input value={step.url} onChange={(e) => update({ url: e.target.value })} placeholder="https://hooks.example.com/go" aria-label="URL" />
            </div>
          </div>
          <div>
            <Label>JSON body (optional)</Label>
            <Textarea rows={3} className="w-full text-xs" value={step.body} onChange={(e) => update({ body: e.target.value })} aria-label="JSON body" spellCheck={false} />
          </div>
        </div>
      );
  }
}

function ServiceFields({ step, update }: { step: Extract<Step, { kind: 'service' }>; update: (patch: Partial<Step>) => void }) {
  const { data: instances = [] } = useIntegrations();
  const { data: schemas = {} } = useIntegrationSchemas();
  const instance = instances.find((i) => i.entity_id === step.target);
  const domain = instance?.domain ?? step.service.split('.')[0];
  const services = schemas[domain]?.services ?? {};
  const serviceName = step.service.includes('.') ? step.service.split('.').slice(1).join('.') : step.service;
  const fields = services[serviceName]?.fields ?? {};

  function pickIntegration(entityId: string) {
    const picked = instances.find((i) => i.entity_id === entityId);
    update({ target: entityId, service: picked && picked.domain !== domain ? '' : step.service, data: picked && picked.domain !== domain ? {} : step.data });
  }

  function pickService(name: string) {
    update({ service: name ? `${domain}.${name}` : '', data: {} });
  }

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-2">
        <div>
          <Label>Integration</Label>
          <Select value={step.target} onChange={(e) => pickIntegration(e.target.value)} aria-label="Integration">
            <option value="">Choose…</option>
            {instances.map((i) => (
              <option key={i.entity_id} value={i.entity_id}>
                {i.display_name} ({i.entity_id})
              </option>
            ))}
            {step.target && !instance && <option value={step.target}>{step.target} (not found)</option>}
          </Select>
        </div>
        <div>
          <Label>Service</Label>
          <Select value={serviceName} onChange={(e) => pickService(e.target.value)} aria-label="Service" disabled={!domain}>
            <option value="">Choose…</option>
            {Object.entries(services).map(([name, spec]) => (
              <option key={name} value={name}>
                {name}
                {spec.description ? ` — ${spec.description}` : ''}
              </option>
            ))}
            {serviceName && !services[serviceName] && <option value={serviceName}>{serviceName} (custom)</option>}
          </Select>
        </div>
      </div>
      {Object.entries(fields).map(([name, spec]) => (
        <FieldInput
          key={name}
          name={name}
          type={spec.type}
          required={Boolean(spec.required)}
          value={step.data[name]}
          onChange={(v) => update({ data: { ...step.data, [name]: v } })}
        />
      ))}
      {Object.keys(step.data)
        .filter((k) => !(k in fields))
        .map((k) => (
          <FieldInput key={k} name={k} type="string" required={false} value={step.data[k]} onChange={(v) => update({ data: { ...step.data, [k]: v } })} />
        ))}
    </div>
  );
}

function FieldInput({
  name,
  type,
  required,
  value,
  onChange,
}: {
  name: string;
  type: string;
  required: boolean;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const label = `${name}${required ? ' *' : ''}`;
  if (type === 'boolean') {
    return (
      <label className="flex items-center gap-2 text-sm text-vmd-text">
        <input type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} aria-label={name} />
        {label}
      </label>
    );
  }
  const text = value === undefined || value === null ? '' : typeof value === 'string' ? value : JSON.stringify(value);
  return (
    <div>
      <Label>
        {label} <span className="text-vmd-textSubtle">({type})</span>
      </Label>
      <Input
        type={type === 'integer' || type === 'number' ? 'number' : 'text'}
        value={text}
        aria-label={name}
        placeholder={type === 'array' ? '["/address", 1]' : undefined}
        onChange={(e) => onChange(coerceFieldValue(type, e.target.value))}
      />
    </div>
  );
}

function ScriptFields({ step, update }: { step: Extract<Step, { kind: 'script' }>; update: (patch: Partial<Step>) => void }) {
  const { data: scripts = [] } = useScripts();
  return (
    <div>
      <Label>Script</Label>
      <Select value={step.name} onChange={(e) => update({ name: e.target.value })} aria-label="Script">
        <option value="">Choose…</option>
        {scripts.map((s) => (
          <option key={s.name} value={s.name}>
            {s.name}
          </option>
        ))}
        {step.name && !scripts.some((s) => s.name === step.name) && <option value={step.name}>{step.name} (not found)</option>}
      </Select>
    </div>
  );
}

function SpotlightFields({ step, update }: { step: Extract<Step, { kind: 'spotlight' }>; update: (patch: Partial<Step>) => void }) {
  const { data: overlays = [] } = useOverlays();
  return (
    <div className="grid grid-cols-2 gap-2">
      <div>
        <Label>Team (blank = back to the match view)</Label>
        <Input value={step.team} onChange={(e) => update({ team: e.target.value })} placeholder="1234A" aria-label="Spotlight team" />
      </div>
      <div>
        <Label>Overlay</Label>
        <Select value={step.overlay} onChange={(e) => update({ overlay: e.target.value })} aria-label="Overlay">
          <option value="">All overlays</option>
          {overlays.map((o) => (
            <option key={o.entity_id} value={o.entity_id}>
              {o.display_name}
            </option>
          ))}
        </Select>
      </div>
    </div>
  );
}
