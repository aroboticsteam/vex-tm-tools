import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { TeamAlertCard } from './TeamAlertCard';
import { alertSummary, parseTeams } from '../../lib/teams';

const apiFetch = vi.fn();
vi.mock('../../lib/http', () => ({ apiFetch: (...a: unknown[]) => apiFetch(...a) }));

function renderCard(initial = '') {
  const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TeamAlertCard initialTeams={initial} />
    </QueryClientProvider>,
  );
}

describe('parseTeams', () => {
  it('splits, upper-cases and dedupes', () => {
    expect(parseTeams('1234a, 5678b  1234A;9999z')).toEqual(['1234A', '5678B', '9999Z']);
    expect(parseTeams('  ')).toEqual([]);
  });
});

describe('alertSummary', () => {
  it('flags unreached teams as an error', () => {
    expect(alertSummary({ rooms: 1, pushed: 2, unreached_teams: [] })).toEqual({
      text: 'Sent to 1 room display, 2 phones',
      kind: 'success',
    });
    const r = alertSummary({ rooms: 0, pushed: 0, unreached_teams: ['1A', '2B'] });
    expect(r.kind).toBe('error');
    expect(r.text).toContain('Not reached: 1A, 2B');
  });
});

describe('TeamAlertCard', () => {
  beforeEach(() => apiFetch.mockReset());

  it('disables send until teams are entered and custom has a message', () => {
    renderCard();
    const btn = screen.getByRole('button', { name: /alert/i });
    expect(btn).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Teams to alert'), { target: { value: '1234A' } });
    expect(btn).toBeEnabled();
    fireEvent.change(screen.getByLabelText('Alert reason'), { target: { value: 'custom' } });
    expect(btn).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Alert message'), { target: { value: 'Hi' } });
    expect(btn).toBeEnabled();
  });

  it('posts the parsed teams and reason', async () => {
    apiFetch.mockResolvedValue({ rooms: 1, pushed: 0, unreached_teams: [] });
    renderCard('1234a 5678b');
    fireEvent.click(screen.getByRole('button', { name: /alert 2 teams/i }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    expect(apiFetch).toHaveBeenCalledWith('/api/v1/notifications/team-alert', {
      method: 'POST',
      body: { teams: ['1234A', '5678B'], reason: 'pit_admin', message: null },
    });
  });
});
