import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// The OBS overlay is a standalone page (backend/static/overlay.html), not part of
// the SPA, so load its script into jsdom and drive it through its test hooks.
const html = readFileSync(resolve(process.cwd(), '../backend/static/overlay.html'), 'utf8');
const script = html.slice(html.lastIndexOf('<script>') + 8, html.lastIndexOf('</script>'));

type Hooks = {
  renderMatch: (p: unknown) => Promise<void>;
  setSpotlight: (n: string | null) => Promise<void>;
  bannerText: (p: unknown) => string;
  flagFor: (c: string) => string;
};

const FEED = {
  show_robot_name: false,
  teams: {
    '1A': { team_name: 'Alpha', country: 'United Kingdom', bot_url: 'http://x/1a.webm', team_url: 'http://x/1a-team.webm' },
    '2B': { team_name: 'Bravo', country: 'Turkey' },
    '3C': { team_name: 'Charlie', country: 'Canada' },
    '4D': { team_name: 'Delta', country: 'Narnia' },
  },
};

function load(): Hooks {
  document.body.innerHTML = '<div id="stage"><div id="grid"></div></div>';
  (window as unknown as Record<string, unknown>).__overlayTestHooks = undefined;
  new Function(script)();
  return (window as unknown as { __overlayTestHooks: Hooks }).__overlayTestHooks;
}

beforeEach(() => {
  vi.stubGlobal('WebSocket', class { onclose: unknown; close() {} });
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => FEED })));
});

describe('overlay page layouts (#13)', () => {
  it('formats the match banner', () => {
    const h = load();
    expect(h.bannerText({ round: 'QUAL', matchNum: 191 })).toBe('Q 191');
    expect(h.bannerText({ round: 'SF', matchNum: 2 })).toBe('SF 2');
    expect(h.bannerText({})).toBe('');
  });

  it('maps countries to ISO codes', () => {
    const h = load();
    expect(h.flagFor('United Kingdom')).toBe('GB');
    expect(h.flagFor('Türkiye')).toBe('TR');
    expect(h.flagFor('Narnia')).toBe('');
  });

  it('renders VRC as 2x2 with VS, red left and blue right', async () => {
    const h = load();
    await h.renderMatch({ round: 'QUAL', matchNum: 7, redTeams: ['1A', '2B'], blueTeams: ['3C', '4D'] });
    const grid = document.getElementById('grid')!;
    expect(grid.classList.contains('mode-vrc')).toBe(true);
    expect(grid.querySelectorAll('.cell')).toHaveLength(4);
    expect(grid.querySelectorAll('.cell.right .panel.blue')).toHaveLength(2);
    expect(grid.querySelectorAll('.cell:not(.right) .panel.red')).toHaveLength(2);
    expect(grid.querySelector('.vs')?.textContent).toBe('VS');
    expect(grid.querySelector('.banner')?.textContent).toBe('Q 7');
    expect(grid.querySelectorAll('.flag')).toHaveLength(3); // Narnia has no flag
    expect(grid.querySelector('video')?.getAttribute('src')).toBe('http://x/1a.webm');
  });

  it('renders IQ as two light-blue cells with no VS', async () => {
    const h = load();
    await h.renderMatch({ round: 'QUAL', matchNum: 42, redTeams: ['1A'], blueTeams: ['2B'] });
    const grid = document.getElementById('grid')!;
    expect(grid.classList.contains('mode-iq')).toBe(true);
    expect(grid.querySelectorAll('.cell.iq')).toHaveLength(2);
    expect(grid.querySelectorAll('.panel.red')).toHaveLength(0);
    expect(grid.querySelector('.vs')).toBeNull();
  });

  it('renders the single-team spotlight with bot, label bar and team clip', async () => {
    const h = load();
    await h.setSpotlight('1A');
    const grid = document.getElementById('grid')!;
    expect(grid.classList.contains('mode-finals')).toBe(true);
    expect(grid.querySelector('.bar .number')?.textContent).toBe('1A');
    expect(grid.querySelector('.bar .name')?.textContent).toBe('Alpha');
    expect(grid.querySelector('.bar .flag')).not.toBeNull();
    expect(grid.querySelector('.teamvid video')?.getAttribute('src')).toBe('http://x/1a-team.webm');
  });
});
