import { act, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ButtonPagesPage } from './ButtonPages';
import { useUiStore } from '../stores/ui';

const press = vi.fn();
const reorderButtons = vi.fn();
const reorderPages = vi.fn();
const noop = { mutate: vi.fn(), isPending: false };

const pages = [
  {
    id: 'p1',
    name: 'Field',
    position: 0,
    columns: 3,
    buttons: [
      { id: 'b1', page_id: 'p1', label: 'Flash', color: '#112233', position: 0, action_yaml: '- delay: 1\n', icon: '💡', confirm: false },
      { id: 'b2', page_id: 'p1', label: 'Reset', color: '#223344', position: 1, action_yaml: '- delay: 1\n', icon: null, confirm: true },
      { id: 'b3', page_id: 'p1', label: 'Third', color: '#334455', position: 2, action_yaml: '- delay: 1\n', icon: null, confirm: false },
    ],
  },
  { id: 'p2', name: 'Stage', position: 1, columns: 4, buttons: [] },
];

vi.mock('../api/buttonPages', () => ({
  useButtonPages: () => ({ data: pages, isLoading: false }),
  usePressButton: () => ({ mutate: press, isPending: false }),
  useReorderButtons: () => ({ mutate: reorderButtons }),
  useReorderPages: () => ({ mutate: reorderPages }),
  useCreateButton: () => noop,
  useUpdateButton: () => noop,
  useDeleteButton: () => noop,
  useCreatePage: () => noop,
  useUpdatePage: () => noop,
  useDeletePage: () => noop,
}));
vi.mock('../api/integrations', () => ({ useIntegrations: () => ({ data: [] }), useIntegrationSchemas: () => ({ data: {} }) }));
vi.mock('../api/scripts', () => ({ useScripts: () => ({ data: [] }) }));
vi.mock('../api/overlays', () => ({ useOverlays: () => ({ data: [] }) }));
vi.mock('../hooks/usePermission', () => ({ usePermission: () => true }));

describe('ButtonPagesPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useUiStore.setState({ toasts: [] } as never);
  });

  it('renders icons, pages and a grid sized by the page columns', () => {
    render(<ButtonPagesPage />);
    expect(screen.getByRole('tab', { name: 'Field' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: 'Stage' })).toBeInTheDocument();
    expect(screen.getAllByTestId('pad-button')).toHaveLength(3);
    expect(screen.getByText('💡')).toBeInTheDocument();
  });

  it('shows running, then success/failed feedback after a press', async () => {
    vi.useFakeTimers();
    try {
      render(<ButtonPagesPage />);
      const flash = screen.getAllByTestId('pad-button')[0];
      fireEvent.click(flash);
      expect(press).toHaveBeenCalledTimes(1);
      const [id, callbacks] = press.mock.calls[0];
      expect(id).toBe('b1');

      expect(screen.getAllByTestId('pad-button')[0]).toHaveAttribute('data-state', 'running');
      act(() => {
        callbacks.onSuccess({ status: 'success', error: null });
        callbacks.onSettled();
      });
      expect(screen.getAllByTestId('pad-button')[0]).toHaveAttribute('data-state', 'success');
      act(() => vi.advanceTimersByTime(1600));
      expect(screen.getAllByTestId('pad-button')[0]).toHaveAttribute('data-state', 'idle');

      act(() => {
        callbacks.onSuccess({ status: 'failed', error: 'ATEM offline' });
        callbacks.onSettled();
      });
      expect(screen.getAllByTestId('pad-button')[0]).toHaveAttribute('data-state', 'failed');
      expect(screen.getByText('✕')).toBeInTheDocument();
      expect(useUiStore.getState().toasts.some((t) => /ATEM offline/.test(t.message))).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it('asks for confirmation on risky buttons and only presses when accepted', () => {
    const confirmSpy = vi.spyOn(window, 'confirm');
    render(<ButtonPagesPage />);
    const reset = screen.getAllByTestId('pad-button')[1];

    confirmSpy.mockReturnValueOnce(false);
    fireEvent.click(reset);
    expect(press).not.toHaveBeenCalled();

    confirmSpy.mockReturnValueOnce(true);
    fireEvent.click(reset);
    expect(press).toHaveBeenCalledTimes(1);
    expect(press.mock.calls[0][0]).toBe('b2');
    confirmSpy.mockRestore();
  });

  it('reorders buttons and pages by drag and drop in edit mode only', async () => {
    const user = userEvent.setup();
    render(<ButtonPagesPage />);
    const [first, , third] = screen.getAllByTestId('pad-button');
    expect(first).toHaveAttribute('draggable', 'false');

    await user.click(screen.getByRole('button', { name: /edit/i }));
    expect(screen.getAllByTestId('pad-button')[0]).toHaveAttribute('draggable', 'true');

    fireEvent.dragStart(screen.getAllByTestId('pad-button')[2]);
    fireEvent.dragOver(screen.getAllByTestId('pad-button')[0]);
    fireEvent.drop(screen.getAllByTestId('pad-button')[0]);
    expect(reorderButtons).toHaveBeenCalledTimes(1);
    expect(reorderButtons.mock.calls[0][0]).toEqual({ pageId: 'p1', ids: ['b3', 'b1', 'b2'] });
    void first;
    void third;

    fireEvent.dragStart(screen.getByRole('tab', { name: 'Stage' }));
    fireEvent.drop(screen.getByRole('tab', { name: 'Field' }));
    expect(reorderPages).toHaveBeenCalledWith(['p2', 'p1'], expect.anything());
  });

  it('opens the editor with the visual builder when a button is clicked in edit mode', async () => {
    const user = userEvent.setup();
    render(<ButtonPagesPage />);
    await user.click(screen.getByRole('button', { name: /edit/i }));
    await user.click(screen.getAllByTestId('pad-button')[0]);
    expect(screen.getByText('Edit Flash')).toBeInTheDocument();
    expect(screen.getByTestId('visual-editor')).toBeInTheDocument();
    expect(screen.getByLabelText('Delay')).toBeInTheDocument();
    expect(screen.getByLabelText('Icon')).toHaveValue('💡');
  });
});
