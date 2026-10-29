import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { TimerCard } from './TimerCard';

const mutate = vi.fn();
vi.mock('../../api/comms', () => ({ useTimerControl: () => ({ mutate, isPending: false }) }));

describe('TimerCard', () => {
  beforeEach(() => mutate.mockClear());

  it('starts a countdown for the chosen audience with cue thresholds', async () => {
    const user = userEvent.setup();
    render(<TimerCard audience={{ roles: ['emcee'] }} />);
    await user.clear(screen.getByLabelText('Minutes'));
    await user.type(screen.getByLabelText('Minutes'), '2');
    await user.clear(screen.getByLabelText('Seconds'));
    await user.type(screen.getByLabelText('Seconds'), '30');
    await user.type(screen.getByLabelText('Timer label'), 'Finals break');
    await user.click(screen.getByRole('button', { name: /start/i }));
    expect(mutate.mock.calls[0][0]).toEqual({
      roles: ['emcee'],
      action: 'start',
      mode: 'countdown',
      label: 'Finals break',
      duration_seconds: 150,
      warn_seconds: 120,
      critical_seconds: 30,
    });
  });

  it('quick picks fill the duration; count-up needs no duration', async () => {
    const user = userEvent.setup();
    render(<TimerCard audience={{}} />);
    await user.click(screen.getByRole('button', { name: '10 min' }));
    await user.click(screen.getByRole('button', { name: /start/i }));
    expect(mutate.mock.calls[0][0]).toMatchObject({ action: 'start', duration_seconds: 600 });

    await user.selectOptions(screen.getByLabelText('Timer type'), 'countup');
    expect(screen.queryByLabelText('Minutes')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /start/i }));
    expect(mutate.mock.calls[1][0]).toEqual({ action: 'start', mode: 'countup', label: '' });
  });

  it('blocks starting with a zero duration or red-before-amber thresholds', async () => {
    const user = userEvent.setup();
    render(<TimerCard audience={{}} />);
    const start = screen.getByRole('button', { name: /start/i });
    await user.clear(screen.getByLabelText('Minutes'));
    await user.type(screen.getByLabelText('Minutes'), '0');
    expect(start).toBeDisabled();
    await user.clear(screen.getByLabelText('Minutes'));
    await user.type(screen.getByLabelText('Minutes'), '3');
    expect(start).toBeEnabled();
    await user.clear(screen.getByLabelText('Red at'));
    await user.type(screen.getByLabelText('Red at'), '500');
    expect(start).toBeDisabled();
    expect(screen.getByText(/Red must not come before amber/)).toBeInTheDocument();
  });

  it('sends pause, resume, adjust and stop controls', async () => {
    const user = userEvent.setup();
    render(<TimerCard audience={{ stations: ['cam-1'] }} />);
    await user.click(screen.getByRole('button', { name: /pause/i }));
    await user.click(screen.getByRole('button', { name: /resume/i }));
    await user.click(screen.getByRole('button', { name: '+1 min' }));
    await user.click(screen.getByRole('button', { name: '−1 min' }));
    await user.click(screen.getByRole('button', { name: /stop/i }));
    expect(mutate.mock.calls.map((c) => c[0])).toEqual([
      { stations: ['cam-1'], action: 'pause' },
      { stations: ['cam-1'], action: 'resume' },
      { stations: ['cam-1'], action: 'adjust', adjust_seconds: 60 },
      { stations: ['cam-1'], action: 'adjust', adjust_seconds: -60 },
      { stations: ['cam-1'], action: 'stop' },
    ]);
  });
});
