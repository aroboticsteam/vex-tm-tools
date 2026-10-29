import { describe, expect, it } from 'vitest';
import {
  chainProblems,
  coerceFieldValue,
  moveItem,
  newStep,
  stepProblems,
  stepsToYaml,
  yamlToSteps,
  type Step,
} from './actionBuilder';

describe('stepsToYaml / yamlToSteps', () => {
  it('round-trips every supported step kind', () => {
    const yaml = [
      '- service: wled.flash',
      '  target: wled.field1',
      '  data:',
      '    count: 3',
      '    color: "#FF0000"',
      '- delay: 00:00:02',
      '- script: reset_lights',
      '- spotlight:',
      '    team: 1234A',
      '    overlay: overlay-main',
      '- http:',
      '    url: https://hooks.example.com/go',
      '    method: POST',
      '    json:',
      '      who: me',
      '',
    ].join('\n');
    const parsed = yamlToSteps(yaml);
    expect(parsed.ok).toBe(true);
    expect(parsed.steps.map((s) => s.kind)).toEqual(['service', 'delay', 'script', 'spotlight', 'http']);
    const again = yamlToSteps(stepsToYaml(parsed.steps));
    expect(again.ok).toBe(true);
    expect(again.steps.map(({ id: _id, ...rest }) => rest)).toEqual(parsed.steps.map(({ id: _id, ...rest }) => rest));
  });

  it('refuses YAML the builder cannot represent without loss', () => {
    expect(yamlToSteps('- condition: "{{ true }}"\n').ok).toBe(false);
    expect(yamlToSteps('- service: obs.x\n  target_tag: tag\n').ok).toBe(false);
    expect(yamlToSteps('- repeat:\n    count: 2\n    sequence: []\n').ok).toBe(false);
    expect(yamlToSteps('- service: [unclosed').ok).toBe(false);
    expect(yamlToSteps('- just a string\n').ok).toBe(false);
  });

  it('treats empty YAML as an empty chain and omits blank optional data', () => {
    expect(yamlToSteps('')).toEqual({ ok: true, steps: [] });
    expect(stepsToYaml([])).toBe('');
    const step: Step = { ...(newStep('service') as Extract<Step, { kind: 'service' }>), service: 'wled.flash', target: 'w1', data: { color: '', count: 2 } };
    expect(stepsToYaml([step])).toBe('- service: wled.flash\n  target: w1\n  data:\n    count: 2\n');
  });

  it('serialises a blank spotlight team as null (back to match view)', () => {
    const step = { ...(newStep('spotlight') as Extract<Step, { kind: 'spotlight' }>) };
    expect(stepsToYaml([step])).toContain('team: null');
    expect(yamlToSteps(stepsToYaml([step])).steps[0]).toMatchObject({ kind: 'spotlight', team: '' });
  });
});

describe('validation helpers', () => {
  it('flags incomplete steps', () => {
    expect(stepProblems(newStep('service'))).toBe('Pick a service');
    expect(stepProblems(newStep('script'))).toBe('Pick a script');
    expect(stepProblems({ ...newStep('delay'), delay: 'soon' } as Step)).toMatch(/Delay/);
    expect(stepProblems({ ...newStep('delay'), delay: '1.5' } as Step)).toBeNull();
    expect(stepProblems({ ...newStep('http'), url: 'ftp://x' } as Step)).toMatch(/URL/);
    expect(stepProblems({ ...newStep('http'), url: 'https://x.example', body: '{bad' } as Step)).toMatch(/JSON/);
    expect(stepProblems({ ...newStep('http'), url: 'https://x.example', body: '{"a":1}' } as Step)).toBeNull();
    expect(chainProblems([newStep('service'), newStep('delay')])).toEqual(['Step 1: Pick a service']);
  });

  it('coerces field values by declared type', () => {
    expect(coerceFieldValue('integer', '42')).toBe(42);
    expect(coerceFieldValue('integer', 'x')).toBe('x');
    expect(coerceFieldValue('number', '1.5')).toBe(1.5);
    expect(coerceFieldValue('array', '["/a", 1]')).toEqual(['/a', 1]);
    expect(coerceFieldValue('array', '[oops')).toBe('[oops');
    expect(coerceFieldValue('string', '')).toBe('');
  });

  it('moves items without mutating or going out of range', () => {
    const list = ['a', 'b', 'c'];
    expect(moveItem(list, 0, 2)).toEqual(['b', 'c', 'a']);
    expect(moveItem(list, 2, 0)).toEqual(['c', 'a', 'b']);
    expect(moveItem(list, 1, 1)).toBe(list);
    expect(moveItem(list, 0, 5)).toBe(list);
    expect(list).toEqual(['a', 'b', 'c']);
  });
});
