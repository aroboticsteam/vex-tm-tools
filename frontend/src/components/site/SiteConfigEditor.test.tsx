import { useState } from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SiteConfigEditor } from './SiteConfigEditor';
import type { SiteConfig } from '../../lib/siteConfig';

const upload = vi.fn();
vi.mock('../../api/site', () => ({
  usePublicOverview: () => ({ data: { map: null, sponsors: [] } }),
  useUploadSiteImage: () => ({ mutate: upload, isPending: false }),
}));

function Harness({ initial = {} as SiteConfig }) {
  const [value, setValue] = useState<SiteConfig>(initial);
  return (
    <>
      <SiteConfigEditor value={value} onChange={setValue} />
      <pre data-testid="out">{JSON.stringify(value)}</pre>
    </>
  );
}
const out = () => JSON.parse(screen.getByTestId('out').textContent!) as SiteConfig;

describe('SiteConfigEditor', () => {
  beforeEach(() => upload.mockReset());

  it('edits title, next competition and keeps unknown keys', async () => {
    const user = userEvent.setup();
    render(<Harness initial={{ custom: 1 } as SiteConfig} />);
    await user.type(screen.getByLabelText('Site title'), 'QE Battle');
    await user.type(screen.getByLabelText('Next competition name'), 'Nationals');
    await user.type(screen.getByLabelText('Next competition date'), '2027-01-10');
    await user.type(screen.getByLabelText('Teams attending'), '1234a, 5678b');
    expect(out()).toMatchObject({ custom: 1, title: 'QE Battle', next_competition: { name: 'Nationals', date: '2027-01-10', teams: ['1234A', '5678B'] } });
  });

  it('adds, edits and removes sponsors', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole('button', { name: /add sponsor/i }));
    await user.type(screen.getByLabelText('Sponsor 1 name'), 'Kingston');
    await user.type(screen.getByLabelText('Sponsor 1 link'), 'https://k.example');
    expect(out().sponsors).toEqual([{ name: 'Kingston', url: 'https://k.example' }]);
    await user.click(screen.getByRole('button', { name: /remove sponsor 1/i }));
    expect(out().sponsors).toEqual([]);
  });

  it('uploads a sponsor logo and stores only its key', async () => {
    render(<Harness initial={{ sponsors: [{ name: 'Kingston' }] }} />);
    const file = new File([new Uint8Array([1, 2, 3])], 'logo.png', { type: 'image/png' });
    fireEvent.change(screen.getByLabelText('Upload logo 1'), { target: { files: [file] } });
    expect(upload).toHaveBeenCalledTimes(1);
    expect(upload.mock.calls[0][0]).toBe(file);
    upload.mock.calls[0][1].onSuccess({ key: 'abc.png', url: 'https://s3.test/abc.png' });
    await waitFor(() => expect(out().sponsors).toEqual([{ name: 'Kingston', logo_key: 'abc.png' }]));
    expect(screen.getByTestId('sponsor-row').querySelector('img')).toHaveAttribute('src', 'https://s3.test/abc.png');
  });

  it('places a room pin by clicking the uploaded map', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    expect(screen.getByText(/Upload a map image/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Upload map image'), { target: { files: [new File(['x'], 'map.png', { type: 'image/png' })] } });
    upload.mock.calls[0][1].onSuccess({ key: 'map.png', url: 'https://s3.test/map.png' });
    expect(await screen.findByTestId('map-canvas')).toBeInTheDocument();
    expect(out().map?.image_key).toBe('map.png');

    await user.click(screen.getByRole('button', { name: /add room pin/i }));
    await user.type(screen.getByLabelText('Room 1 name'), 'Main hall');
    const img = screen.getByAltText('Event map');
    img.getBoundingClientRect = () => ({ left: 0, top: 0, width: 200, height: 100, right: 200, bottom: 100, x: 0, y: 0, toJSON: () => ({}) });
    fireEvent.click(screen.getByTestId('map-canvas'), { clientX: 50, clientY: 25 });
    expect(out().map?.rooms).toEqual([{ name: 'Main hall', x: 25, y: 25 }]);

    await user.click(screen.getByRole('button', { name: /remove room 1/i }));
    expect(out().map?.rooms).toEqual([]);
  });

  it('edits leaderboard timing, recording template and the bus tracker', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.clear(screen.getByLabelText('Seconds per panel'));
    await user.type(screen.getByLabelText('Seconds per panel'), '30');
    await user.type(screen.getByLabelText('Recording URL template'), 'https://r.example/{{label}');
    await user.type(screen.getByLabelText('Bus tracker embed URL'), 'https://bus.example');
    await user.click(screen.getByRole('button', { name: /add bus/i }));
    await user.type(screen.getByLabelText('Bus 1 name'), 'Bus 1');
    await user.type(screen.getByLabelText('Bus 1 status'), 'At school');
    expect(out()).toMatchObject({
      leaderboard: { panel_seconds: 30 },
      recording_url_template: 'https://r.example/{label}',
      bus_tracker: { embed_url: 'https://bus.example', buses: [{ name: 'Bus 1', status: 'At school' }] },
    });
  });

  it('lists problems for unsafe links and disables inputs when read-only', async () => {
    const user = userEvent.setup();
    const { unmount } = render(<Harness />);
    await user.type(screen.getByLabelText('Event page link'), 'javascript:alert(1)');
    expect(within(screen.getByTestId('site-problems')).getByText(/Next competition link/)).toBeInTheDocument();
    unmount();

    render(<SiteConfigEditor value={{ title: 'X' }} onChange={() => {}} disabled />);
    expect(screen.getByLabelText('Site title')).toBeDisabled();
    expect(screen.getByRole('button', { name: /add sponsor/i })).toBeDisabled();
  });
});
