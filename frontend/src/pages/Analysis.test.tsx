import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AnalysisPage } from './Analysis';

const mark = vi.fn();
const play = vi.fn();
const control = vi.fn();
let perms = true;

const moments = [
  { id: 'm1', number: 1, length_s: 5, status: 'READY', end_offset_ms: 1, ready_ms: 3000, requested_at: '', error: null },
  { id: 'm2', number: 2, length_s: 10, status: 'REQUESTED', end_offset_ms: null, ready_ms: null, requested_at: '', error: null },
];
const recordings = [
  { id: 'r1', match_key: 'k1', match_type: 'QUAL', match_number: '1', teams: ['1234A', '5678B'], alliances: null, status: 'READY', started_at: '2026-01-01T00:01', stopped_at: null, duration_s: 60, size_bytes: 1, error: null, moments: [] },
  { id: 'r2', match_key: 'k2', match_type: 'QUAL', match_number: '2', teams: ['9999C', '5678B'], alliances: null, status: 'READY', started_at: '2026-01-01T00:02', stopped_at: null, duration_s: 60, size_bytes: 1, error: null, moments },
];

vi.mock('../hooks/usePermission', () => ({ usePermission: () => perms }));
vi.mock('../api/capture', () => ({
  useCaptureRecordings: () => ({ data: recordings }),
  useCaptureStatus: () => ({ data: { connected: true, agent: {}, recording: recordings[1] } }),
  useMarkMoment: () => ({ mutate: mark, isPending: false }),
  useReplayState: () => ({ data: { playing: false } }),
  useReplayPlay: () => ({ mutate: play, isPending: false }),
  useReplayControl: () => ({ mutate: control, isPending: false }),
}));
vi.mock('../api/integrations', () => ({
  useIntegrations: () => ({ data: [{ entity_id: 'obs.main', domain: 'obs', display_name: 'OBS' }] }),
  useCallIntegrationService: () => ({ mutate: vi.fn() }),
}));
vi.mock('../api/teams', () => ({
  useTeams: () => ({ data: [{ team_number: '1234A', robot_name: 'Bolt', cached_stats: { rank: 3 } }] }),
}));


function stubStorage() {
  const store = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  });
}

describe('AnalysisPage', () => {
  beforeEach(() => {
    mark.mockClear();
    play.mockClear();
    control.mockClear();
    perms = true;
    stubStorage();
  });

  it('follows the live recording and lists moments with upload state', () => {
    render(<AnalysisPage />);
    expect(screen.getByTestId('current-match')).toHaveTextContent('Q 2');
    expect(screen.getByLabelText('Moment 1')).toHaveTextContent('Ready');
    expect(screen.getByLabelText('Moment 2')).toHaveTextContent('Uploading');
    expect(screen.getByLabelText('Send moment 1 to program')).toBeEnabled();
    expect(screen.getByLabelText('Send moment 2 to program')).toBeDisabled();
  });

  it('marks via buttons and keyboard, and sends a moment at the chosen speed', async () => {
    render(<AnalysisPage />);
    await userEvent.click(screen.getByText('Mark last 5s'));
    expect(mark.mock.calls[0][0]).toBe(5);
    await userEvent.keyboard('3');
    expect(mark.mock.calls[1][0]).toBe(10);
    await userEvent.click(screen.getByText('0.25×'));
    await userEvent.click(screen.getByLabelText('Send moment 1 to program'));
    expect(play.mock.calls[0][0]).toEqual({ kind: 'moment', id: 'm1', rate: 0.25 });
  });

  it('navigates between matches and shows team stats without predictions', async () => {
    render(<AnalysisPage />);
    await userEvent.click(screen.getByLabelText('Previous match'));
    expect(screen.getByTestId('current-match')).toHaveTextContent('Q 1');
    await userEvent.click(screen.getByRole('tab', { name: 'Stats' }));
    expect(screen.getByLabelText('Stats for 1234A')).toHaveTextContent('rank');
    expect(screen.getByLabelText('Stats for 5678B')).toHaveTextContent('No cached stats');
    expect(screen.queryByText(/predict/i)).not.toBeInTheDocument();
  });

  it('hides everything without analysis:view and disables controls without capture:control', () => {
    perms = false;
    render(<AnalysisPage />);
    expect(screen.getByText(/do not have access/i)).toBeInTheDocument();
  });
});
