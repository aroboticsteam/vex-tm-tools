import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MatchRecordingsCard } from './MatchRecordingsCard';

const save = vi.fn();
const del = vi.fn();
const run = vi.fn();
vi.mock('../../api/site', () => ({
  useMatchRecordings: () => ({
    data: [{ match_key: '1:0:QUAL:1:12', division: '1', label: 'Q12', url: 'https://v.example/12', title: 'Qualification 12' }],
  }),
  useSetMatchRecording: () => ({ mutate: save, isPending: false }),
  useDeleteMatchRecording: () => ({ mutate: del }),
  useImportMatchRecordings: () => ({ mutate: run, isPending: false }),
}));

describe('MatchRecordingsCard', () => {
  beforeEach(() => { save.mockReset(); del.mockReset(); run.mockReset(); });

  it('lists links and removes one', async () => {
    const user = userEvent.setup();
    render(<MatchRecordingsCard canEdit />);
    const row = screen.getByTestId('recording-row');
    expect(within(row).getByText('Qualification 12')).toHaveAttribute('href', 'https://v.example/12');
    await user.click(within(row).getByRole('button', { name: /remove recording Q12/i }));
    expect(del).toHaveBeenCalledWith('1:0:QUAL:1:12', expect.anything());
  });

  it('only enables Link for a division, match and http(s) URL', async () => {
    const user = userEvent.setup();
    render(<MatchRecordingsCard canEdit />);
    const link = screen.getByRole('button', { name: 'Link' });
    expect(link).toBeDisabled();
    await user.type(screen.getByLabelText('Recording match'), 'Q7');
    await user.type(screen.getByLabelText('Recording link'), 'javascript:x');
    expect(link).toBeDisabled();
    await user.clear(screen.getByLabelText('Recording link'));
    await user.type(screen.getByLabelText('Recording link'), 'https://v.example/7');
    expect(link).toBeEnabled();
    await user.click(link);
    expect(save.mock.calls[0][0]).toEqual({ division: '1', label: 'Q7', url: 'https://v.example/7' });
  });

  it('shows per-line problems from a bulk import and stays open', async () => {
    const user = userEvent.setup();
    render(<MatchRecordingsCard canEdit />);
    await user.click(screen.getByRole('button', { name: /bulk import/i }));
    await user.click(screen.getByLabelText('Recording lines'));
    await user.paste('1 Q1 https://v.example/1\noops');
    await user.click(screen.getAllByRole('button', { name: 'Import' }).at(-1)!);
    expect(run.mock.calls[0][0]).toBe('1 Q1 https://v.example/1\noops');
    run.mock.calls[0][1].onSuccess({ saved: 0, errors: [{ line: 2, error: 'Expected …' }] });
    expect(await screen.findByTestId('recording-import-errors')).toHaveTextContent('Line 2');
  });

  it('is read-only without edit permission', () => {
    render(<MatchRecordingsCard canEdit={false} />);
    expect(screen.queryByRole('button', { name: 'Link' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /bulk import/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /remove recording/i })).not.toBeInTheDocument();
  });
});
