import * as yaml from 'js-yaml';

/** One step of a button's action chain, in the shape the visual builder edits.
 * `stepsToYaml` / `yamlToSteps` convert to and from the engine's YAML so the
 * builder and the raw YAML editor stay interchangeable. */
export type Step =
  | { id: string; kind: 'service'; target: string; service: string; data: Record<string, unknown> }
  | { id: string; kind: 'script'; name: string }
  | { id: string; kind: 'delay'; delay: string }
  | { id: string; kind: 'spotlight'; team: string; overlay: string }
  | { id: string; kind: 'http'; method: string; url: string; body: string };

export type StepKind = Step['kind'];

export const HTTP_METHODS = ['POST', 'GET', 'PUT', 'PATCH', 'DELETE'] as const;

let counter = 0;
export function newId(): string {
  counter += 1;
  return `step-${counter}`;
}

export function newStep(kind: StepKind): Step {
  const id = newId();
  switch (kind) {
    case 'service':
      return { id, kind, target: '', service: '', data: {} };
    case 'script':
      return { id, kind, name: '' };
    case 'delay':
      return { id, kind, delay: '00:00:01' };
    case 'spotlight':
      return { id, kind, team: '', overlay: '' };
    case 'http':
      return { id, kind, method: 'POST', url: '', body: '' };
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Drops empty values so optional service fields left blank aren't sent. */
function cleanData(data: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(data)) {
    if (v === '' || v === undefined || v === null || (typeof v === 'number' && Number.isNaN(v))) continue;
    out[k] = v;
  }
  return out;
}

function stepToAction(step: Step): Record<string, unknown> {
  switch (step.kind) {
    case 'service': {
      const out: Record<string, unknown> = { service: step.service };
      if (step.target) out.target = step.target;
      const data = cleanData(step.data);
      if (Object.keys(data).length) out.data = data;
      return out;
    }
    case 'script':
      return { script: step.name };
    case 'delay':
      return { delay: step.delay };
    case 'spotlight': {
      const spec: Record<string, unknown> = { team: step.team.trim() === '' ? null : step.team.trim() };
      if (step.overlay) spec.overlay = step.overlay;
      return { spotlight: spec };
    }
    case 'http': {
      const spec: Record<string, unknown> = { url: step.url, method: step.method };
      if (step.body.trim()) {
        try {
          spec.json = JSON.parse(step.body);
        } catch {
          spec.json = step.body; // invalid JSON is surfaced by `stepProblems`; keep the text so nothing is lost
        }
      }
      return { http: spec };
    }
  }
}

export function stepsToYaml(steps: Step[]): string {
  return steps.length ? yaml.dump(steps.map(stepToAction), { lineWidth: 120 }) : '';
}

/** Parses a button's YAML back into builder steps. `ok: false` means the
 * YAML uses something the builder can't represent faithfully (conditions,
 * repeats, tag targeting…), so the caller should stay in YAML mode rather
 * than risk dropping part of the chain. */
export function yamlToSteps(text: string): { ok: boolean; steps: Step[] } {
  if (text.trim() === '') return { ok: true, steps: [] };
  let loaded: unknown;
  try {
    loaded = yaml.load(text);
  } catch {
    return { ok: false, steps: [] };
  }
  if (loaded === undefined || loaded === null) return { ok: true, steps: [] };
  const list = Array.isArray(loaded) ? loaded : [loaded];
  const steps: Step[] = [];
  for (const raw of list) {
    if (!isRecord(raw)) return { ok: false, steps: [] };
    const keys = Object.keys(raw);
    const only = (...allowed: string[]) => keys.every((k) => allowed.includes(k));
    if ('service' in raw && typeof raw.service === 'string' && only('service', 'target', 'data')) {
      if (raw.target !== undefined && typeof raw.target !== 'string') return { ok: false, steps: [] };
      if (raw.data !== undefined && !isRecord(raw.data)) return { ok: false, steps: [] };
      steps.push({ id: newId(), kind: 'service', service: raw.service, target: (raw.target as string) ?? '', data: (raw.data as Record<string, unknown>) ?? {} });
    } else if ('script' in raw && typeof raw.script === 'string' && only('script')) {
      steps.push({ id: newId(), kind: 'script', name: raw.script });
    } else if ('delay' in raw && only('delay') && (typeof raw.delay === 'string' || typeof raw.delay === 'number')) {
      steps.push({ id: newId(), kind: 'delay', delay: String(raw.delay) });
    } else if ('spotlight' in raw && only('spotlight') && isRecord(raw.spotlight)) {
      const spec = raw.spotlight;
      if (!Object.keys(spec).every((k) => ['team', 'overlay'].includes(k))) return { ok: false, steps: [] };
      steps.push({ id: newId(), kind: 'spotlight', team: spec.team == null ? '' : String(spec.team), overlay: spec.overlay == null ? '' : String(spec.overlay) });
    } else if ('http' in raw && only('http') && (isRecord(raw.http) || typeof raw.http === 'string')) {
      const spec = typeof raw.http === 'string' ? { url: raw.http } : (raw.http as Record<string, unknown>);
      if (!Object.keys(spec).every((k) => ['url', 'method', 'json'].includes(k))) return { ok: false, steps: [] };
      steps.push({
        id: newId(),
        kind: 'http',
        url: String(spec.url ?? ''),
        method: String(spec.method ?? 'POST').toUpperCase(),
        body: spec.json === undefined ? '' : typeof spec.json === 'string' ? spec.json : JSON.stringify(spec.json, null, 2),
      });
    } else {
      return { ok: false, steps: [] };
    }
  }
  return { ok: true, steps };
}

/** Human-readable problems that would make the chain fail or misbehave. */
export function stepProblems(step: Step): string | null {
  switch (step.kind) {
    case 'service':
      return step.service ? null : 'Pick a service';
    case 'script':
      return step.name ? null : 'Pick a script';
    case 'delay':
      return /^(\d+:)?\d{1,2}:\d{2}(\.\d+)?$/.test(step.delay) || /^\d+(\.\d+)?$/.test(step.delay)
        ? null
        : 'Delay must be HH:MM:SS or seconds';
    case 'spotlight':
      return step.team.trim() === '' || /^[A-Za-z0-9{}%.\s_-]+$/.test(step.team) ? null : 'Invalid team';
    case 'http': {
      if (!/^https?:\/\//i.test(step.url)) return 'URL must start with http:// or https://';
      if (step.body.trim()) {
        try {
          JSON.parse(step.body);
        } catch {
          return 'Body must be valid JSON';
        }
      }
      return null;
    }
  }
}

export function chainProblems(steps: Step[]): string[] {
  return steps.flatMap((s, i) => {
    const p = stepProblems(s);
    return p ? [`Step ${i + 1}: ${p}`] : [];
  });
}

/** Coerces a text field's value to the service field's declared type. */
export function coerceFieldValue(type: string, raw: string): unknown {
  if (raw === '') return '';
  if (type === 'integer') {
    const n = Number.parseInt(raw, 10);
    return Number.isNaN(n) ? raw : n;
  }
  if (type === 'number' || type === 'float') {
    const n = Number(raw);
    return Number.isNaN(n) ? raw : n;
  }
  if (type === 'array' || type === 'object') {
    try {
      return JSON.parse(raw);
    } catch {
      return raw;
    }
  }
  return raw;
}

export function moveItem<T>(list: T[], from: number, to: number): T[] {
  if (from === to || from < 0 || to < 0 || from >= list.length || to >= list.length) return list;
  const next = [...list];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}
