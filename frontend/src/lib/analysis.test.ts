import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DEFAULT_SETUP, findByNumber, loadSetup, momentLabel, orderRecordings, recordingLabel, saveSetup, stepMatch } from './analysis';
import type { CaptureRecording } from '../types/api';

const rec = (id: string, n: string, at: string, type = 'QUAL'): CaptureRecording => ({
  id, match_key: id, match_type: type, match_number: n, teams: [], alliances: null, status: 'READY',
  started_at: at, stopped_at: null, duration_s: null, size_bytes: null, error: null, moments: [],
});
const list = orderRecordings([rec('c', '3', '2026-01-01T00:03'), rec('a', '1', '2026-01-01T00:01'), rec('b', '2', '2026-01-01T00:02')]);


function stubStorage() {
  const store = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  });
}

describe('analysis helpers', () => {
  beforeEach(stubStorage);

  it('orders oldest first and steps with clamping', () => {
    expect(list.map((r) => r.id)).toEqual(['a', 'b', 'c']);
    expect(stepMatch(list, 'a', 1)?.id).toBe('b');
    expect(stepMatch(list, 'a', -1)?.id).toBe('a');
    expect(stepMatch(list, 'c', 1)?.id).toBe('c');
    expect(stepMatch(list, null, 1)?.id).toBe('a');
    expect(stepMatch(list, null, -1)?.id).toBe('c');
    expect(stepMatch([], null, 1)).toBeNull();
  });

  it('finds by match number, tolerating a Q prefix', () => {
    expect(findByNumber(list, '2')?.id).toBe('b');
    expect(findByNumber(list, 'q 3')?.id).toBe('c');
    expect(findByNumber(list, '99')).toBeNull();
    expect(findByNumber(list, '  ')).toBeNull();
  });

  it('labels', () => {
    expect(recordingLabel(list[0])).toBe('Q 1');
    expect(recordingLabel(rec('x', 'SF-1', '', 'SF'))).toBe('SF SF-1');
    expect(momentLabel(list[0], 2)).toBe('Q 1 · moment 2');
  });

  it('persists setup and falls back on corrupt storage', () => {
    expect(loadSetup()).toEqual(DEFAULT_SETUP);
    saveSetup({ ...DEFAULT_SETUP, replayScene: 'Slo-mo' });
    expect(loadSetup().replayScene).toBe('Slo-mo');
    window.localStorage.setItem('analysis-desk-setup', '{oops');
    expect(loadSetup()).toEqual(DEFAULT_SETUP);
    window.localStorage.setItem('analysis-desk-setup', JSON.stringify({ keys: { q: 7 } }));
    expect(loadSetup().keys).toEqual({ ...DEFAULT_SETUP.keys, q: 7 });
  });
});
