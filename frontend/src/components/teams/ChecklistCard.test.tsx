import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ChecklistCard } from './ChecklistCard';

const mutate = vi.fn();
let canEdit = true;
const rows = [
  { team_number: '1234A', inspection: 'IN_PROGRESS', bot_video: true, team_video: false, done: 1, total: 3, complete: false },
];

vi.mock('../../api/teams', () => ({
  useTeamChecklist: () => ({ data: rows }),
  useUpdateTeam: () => ({ mutate, isPending: false }),
}));
vi.mock('../../hooks/usePermission', () => ({ usePermission: () => canEdit }));

describe('ChecklistCard', () => {
  beforeEach(() => {
    mutate.mockClear();
    canEdit = true;
  });

  it('shows progress and per-item state', () => {
    render(<ChecklistCard teamNumber="1234A" />);
    expect(screen.getByText('Checklist (1/3)')).toBeInTheDocument();
    expect(screen.getByLabelText('Inspection status')).toHaveValue('IN_PROGRESS');
    expect(screen.getByText('360 robot video')).toBeInTheDocument();
    expect(screen.getByText('Team video')).toBeInTheDocument();
  });

  it('saves a new inspection status', async () => {
    render(<ChecklistCard teamNumber="1234A" />);
    await userEvent.selectOptions(screen.getByLabelText('Inspection status'), 'PASSED');
    expect(mutate).toHaveBeenCalledWith({ number: '1234A', body: { inspection_status: 'PASSED' } });
  });

  it('is read-only without teams:edit', () => {
    canEdit = false;
    render(<ChecklistCard teamNumber="1234A" />);
    expect(screen.getByLabelText('Inspection status')).toBeDisabled();
  });

  it('renders nothing for an unknown team', () => {
    const { container } = render(<ChecklistCard teamNumber="9999Z" />);
    expect(container).toBeEmptyDOMElement();
  });
});
