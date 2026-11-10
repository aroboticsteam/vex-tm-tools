import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SettingsPage } from './Settings';

const mutate = vi.fn();
let data: unknown[] = [];

vi.mock('../api/settings', () => ({
  useSettings: () => ({ data }),
  useUpdateSettings: () => ({ mutate, isPending: false }),
  useApiKeys: () => ({ data: [] }),
  useCreateApiKey: () => ({ mutate: vi.fn(), isPending: false }),
  useRevokeApiKey: () => ({ mutate: vi.fn() }),
}));
vi.mock('../hooks/usePermission', () => ({ usePermission: () => true }));
vi.mock('../components/site/MatchRecordingsCard', () => ({ MatchRecordingsCard: () => null }));
vi.mock('../components/site/SiteConfigEditor', () => ({ SiteConfigEditor: () => null }));

describe('SettingsPage competition program', () => {
  beforeEach(() => { mutate.mockReset(); data = [{ key: 'competition', value: { program: 'VIQRC' } }]; });

  it('shows the stored program and saves a change', async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    const select = screen.getByLabelText('VEX program');
    expect(select).toHaveValue('VIQRC');
    await user.selectOptions(select, 'V5RC');
    await user.click(screen.getByRole('button', { name: /save/i }));
    expect(mutate.mock.calls[0][0].competition).toEqual({ value: { program: 'V5RC' } });
  });

  it('defaults to V5RC when unset', () => {
    data = [{ key: 'competition', value: {} }];
    render(<SettingsPage />);
    expect(screen.getByLabelText('VEX program')).toHaveValue('V5RC');
  });
});
