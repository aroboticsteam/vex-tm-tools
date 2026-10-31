import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { TeamRecordingsCard } from './TeamRecordingsCard';

let canView = true;
let data: unknown[] = [];
const useCaptureRecordings = vi.fn((_p: unknown) => ({ data }));

vi.mock('../../api/capture', () => ({ useCaptureRecordings: (p: unknown) => useCaptureRecordings(p) }));
vi.mock('../../hooks/usePermission', () => ({ usePermission: () => canView }));

const rec = (over: Record<string, unknown>) => ({
  id: 'r1', match_key: 'k', match_type: 'QUAL', match_number: '3', teams: ['1234A'],
  alliances: null, status: 'READY', started_at: '2026-10-31T10:00:00Z', stopped_at: null,
  duration_s: 185, size_bytes: 1, error: null, moments: [], ...over,
});

describe('TeamRecordingsCard', () => {
  beforeEach(() => {
    canView = true;
    data = [];
    useCaptureRecordings.mockClear();
  });

  it('queries by team and lists recordings oldest first with a watch link', () => {
    data = [
      rec({ id: 'b', match_number: '9', started_at: '2026-10-31T12:00:00Z' }),
      rec({ id: 'a', match_number: '3' }),
    ];
    render(<TeamRecordingsCard teamNumber="1234A" />);
    expect(useCaptureRecordings).toHaveBeenCalledWith({ team: '1234A', limit: 200 });
    expect(screen.getByText('Match recordings (2)')).toBeInTheDocument();
    const items = screen.getAllByRole('listitem');
    expect(items[0]).toHaveTextContent('Q 3');
    expect(items[1]).toHaveTextContent('Q 9');
    expect(screen.getAllByRole('link')[0]).toHaveAttribute('href', '/api/v1/public/recordings/a/video');
    expect(screen.getAllByRole('link')[0]).toHaveTextContent('Watch 3:05');
  });

  it('shows in-progress recordings without a link', () => {
    data = [rec({ status: 'RECORDING' })];
    render(<TeamRecordingsCard teamNumber="1234A" />);
    expect(screen.getByText('Recording…')).toBeInTheDocument();
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('shows an empty state', () => {
    render(<TeamRecordingsCard teamNumber="1234A" />);
    expect(screen.getByText('No recordings for this team yet.')).toBeInTheDocument();
  });

  it('renders nothing without analysis:view', () => {
    canView = false;
    const { container } = render(<TeamRecordingsCard teamNumber="1234A" />);
    expect(container).toBeEmptyDOMElement();
    expect(useCaptureRecordings).not.toHaveBeenCalled();
  });
});
