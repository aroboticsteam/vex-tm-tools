import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CommsPage } from './Comms';
import { useUiStore } from '../stores/ui';

const m = {
  rotate: vi.fn(),
  setPin: vi.fn(),
  unsecure: vi.fn(),
  reorder: vi.fn(),
  importAgenda: vi.fn(),
  delStation: vi.fn(),
};
const noop = { mutate: vi.fn(), isPending: false };

const stations = [
  { slug: 'emcee-1', name: 'Main emcee', role: 'emcee', secured: false, has_pin: false, connected: true, last_seen: Date.now() / 1000 },
  { slug: 'cam-1', name: 'Camera 1', role: 'camera', secured: true, has_pin: true, connected: false, last_seen: Date.now() / 1000 - 600 },
  { slug: 'fm-1', name: 'Floor', role: 'floor_manager', secured: false, has_pin: false, connected: false, last_seen: null },
];
const agenda = [
  { id: 'a1', scope: 'day', start_time: '09:00', end_time: null, title: 'Doors', details: null, position: 0, announce_lead_minutes: null },
  { id: 'a2', scope: 'day', start_time: '10:00', end_time: null, title: 'Opening', details: null, position: 1, announce_lead_minutes: null },
  { id: 'a3', scope: 'day', start_time: '12:00', end_time: null, title: 'Lunch', details: null, position: 2, announce_lead_minutes: null },
];

vi.mock('../api/comms', () => ({
  useStations: () => ({ data: stations }),
  usePresets: () => ({ data: [] }),
  useInbox: () => ({ data: [] }),
  useAgenda: () => ({ data: agenda }),
  useAckMessage: () => noop,
  useSendMessage: () => noop,
  usePushScreen: () => noop,
  useTimerControl: () => noop,
  useSaveStation: () => noop,
  useSavePreset: () => noop,
  useSaveAgendaItem: () => noop,
  useDeletePreset: () => noop,
  useDeleteAgendaItem: () => noop,
  useDeleteStation: () => ({ mutate: m.delStation }),
  useRotateLink: () => ({ mutate: m.rotate }),
  useSetPin: () => ({ mutate: m.setPin }),
  useUnsecureStation: () => ({ mutate: m.unsecure }),
  useReorderAgenda: () => ({ mutate: m.reorder }),
  useImportAgenda: () => ({ mutate: m.importAgenda, isPending: false }),
  fetchStationLink: vi.fn().mockResolvedValue({ access_key: 'k', path: '/station/cam-1?k=k' }),
}));
vi.mock('../hooks/usePermission', () => ({ usePermission: () => true }));

describe('CommsPage desk console', () => {
  beforeEach(() => {
    Object.values(m).forEach((fn) => fn.mockReset());
    useUiStore.setState({ toasts: [] } as never);
  });

  it('shows presence and security state per station', () => {
    render(<CommsPage />);
    const rows = screen.getAllByTestId('station-row');
    expect(within(rows[0]).getByTestId('presence')).toHaveAttribute('data-connected', 'true');
    expect(within(rows[0]).getByText(/Connected/)).toBeInTheDocument();
    expect(within(rows[1]).getByTestId('presence')).toHaveAttribute('data-connected', 'false');
    expect(within(rows[1]).getByText(/Key \+ PIN/)).toBeInTheDocument();
    expect(within(rows[1]).getByText(/Seen 10 min ago/)).toBeInTheDocument();
    expect(within(rows[2]).getByText(/Never opened/)).toBeInTheDocument();
    expect(within(rows[2]).getByRole('button', { name: /Secure/ })).toBeInTheDocument();
    expect(within(rows[1]).getByRole('button', { name: /Rotate/ })).toBeInTheDocument();
  });

  it('rotates a link after confirmation only', async () => {
    const user = userEvent.setup();
    const confirmSpy = vi.spyOn(window, 'confirm');
    render(<CommsPage />);
    const row = screen.getAllByTestId('station-row')[1];

    confirmSpy.mockReturnValueOnce(false);
    await user.click(within(row).getByRole('button', { name: /Rotate/ }));
    expect(m.rotate).not.toHaveBeenCalled();

    confirmSpy.mockReturnValueOnce(true);
    await user.click(within(row).getByRole('button', { name: /Rotate/ }));
    expect(m.rotate).toHaveBeenCalledWith('cam-1', expect.anything());
    confirmSpy.mockRestore();
  });

  it('validates and sends a PIN (empty removes it)', async () => {
    const user = userEvent.setup();
    const promptSpy = vi.spyOn(window, 'prompt');
    render(<CommsPage />);
    const pinButton = () => within(screen.getAllByTestId('station-row')[0]).getByTitle('Set or remove the PIN');

    promptSpy.mockReturnValueOnce('12ab');
    await user.click(pinButton());
    expect(m.setPin).not.toHaveBeenCalled();
    expect(useUiStore.getState().toasts.some((t) => /4 to 8 digits/.test(t.message))).toBe(true);

    promptSpy.mockReturnValueOnce('4821');
    await user.click(pinButton());
    expect(m.setPin).toHaveBeenLastCalledWith({ slug: 'emcee-1', pin: '4821' }, expect.anything());

    promptSpy.mockReturnValueOnce('');
    await user.click(pinButton());
    expect(m.setPin).toHaveBeenLastCalledWith({ slug: 'emcee-1', pin: null }, expect.anything());

    promptSpy.mockReturnValueOnce(null); // cancelled
    await user.click(pinButton());
    expect(m.setPin).toHaveBeenCalledTimes(2);
    promptSpy.mockRestore();
  });

  it('reorders the agenda by dragging a row', () => {
    render(<CommsPage />);
    const rows = screen.getAllByTestId('agenda-row');
    expect(rows).toHaveLength(3);
    fireEvent.dragStart(rows[2]);
    fireEvent.dragOver(rows[0]);
    fireEvent.drop(rows[0]);
    expect(m.reorder).toHaveBeenCalledWith({ scope: 'day', ids: ['a3', 'a1', 'a2'] }, expect.anything());
  });

  it('checks then imports pasted agenda text, surfacing line errors', async () => {
    const user = userEvent.setup();
    render(<CommsPage />);
    await user.click(screen.getByRole('button', { name: /Import/ }));
    const box = screen.getByLabelText('Agenda text');
    await user.click(box);
    await user.paste('09:00 Doors\nnope');

    await user.click(screen.getByRole('button', { name: 'Check' }));
    expect(m.importAgenda).toHaveBeenLastCalledWith({ scope: 'day', text: '09:00 Doors\nnope', replace: false, dry_run: true }, expect.anything());
    const { onSuccess } = m.importAgenda.mock.calls[0][1];
    onSuccess({ created: 0, items: [], errors: [{ line: 2, error: "Expected 'HH:MM[-HH:MM] Title'" }] });
    expect(await screen.findByTestId('import-errors')).toHaveTextContent('Line 2');

    await user.click(screen.getAllByRole('button', { name: 'Import' }).at(-1)!); // the modal's submit, after the card's opener
    expect(m.importAgenda).toHaveBeenLastCalledWith(expect.objectContaining({ dry_run: false }), expect.anything());
  });
});
