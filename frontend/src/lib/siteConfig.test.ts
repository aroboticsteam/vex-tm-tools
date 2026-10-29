import { describe, expect, it } from 'vitest';
import { isHttpUrl, parseTeamList, percentPosition, siteProblems, tidySite } from './siteConfig';

describe('percentPosition', () => {
  const rect = { left: 100, top: 50, width: 400, height: 200 };
  it('converts a click to percent of the image, one decimal', () => {
    expect(percentPosition({ clientX: 300, clientY: 150 }, rect)).toEqual({ x: 50, y: 50 });
    expect(percentPosition({ clientX: 101, clientY: 51 }, rect)).toEqual({ x: 0.3, y: 0.5 });
  });
  it('clamps clicks outside the image and survives a zero-size rect', () => {
    expect(percentPosition({ clientX: 9999, clientY: -50 }, rect)).toEqual({ x: 100, y: 0 });
    expect(percentPosition({ clientX: 10, clientY: 10 }, { left: 0, top: 0, width: 0, height: 0 })).toEqual({ x: 0, y: 0 });
  });
});

describe('helpers', () => {
  it('parses team lists, upper-casing and de-duplicating', () => {
    expect(parseTeamList('1234a, 5678B;1234A  9999z')).toEqual(['1234A', '5678B', '9999Z']);
    expect(parseTeamList('')).toEqual([]);
  });
  it('recognises only http(s) links', () => {
    expect(isHttpUrl('https://x.example/a')).toBe(true);
    expect(isHttpUrl(' http://x.example ')).toBe(true);
    expect(isHttpUrl('javascript:alert(1)')).toBe(false);
    expect(isHttpUrl('ftp://x')).toBe(false);
    expect(isHttpUrl('https://has space')).toBe(false);
  });
});

describe('siteProblems', () => {
  it('flags unsafe links, bad dates and out-of-range timings', () => {
    const problems = siteProblems({
      next_competition: { name: 'N', url: 'javascript:x', registration_url: 'https://ok.example', date: '10/01/2027' },
      sponsors: [{ name: 'A', url: 'nope' }],
      bus_tracker: { embed_url: 'http://bus.example', link_url: 'ftp://x' },
      recording_url_template: 'file:///{label}',
      leaderboard: { panel_seconds: 1, programme_seconds: 99999 },
    });
    expect(problems).toEqual([
      'Next competition link must start with http:// or https://',
      'Sponsor 1 link must start with http:// or https://',
      'Bus tracker link must start with http:// or https://',
      'Recording URL template must start with http:// or https://',
      'Next competition date must be YYYY-MM-DD',
      'Panel seconds must be between 5 and 3600',
      'Programme seconds must be between 5 and 3600',
    ]);
  });
  it('accepts a clean config and templates with placeholders', () => {
    expect(siteProblems({ recording_url_template: 'https://r.example/{division}/{label}.mp4', next_competition: { name: 'N', date: '2027-01-10' } })).toEqual([]);
    expect(siteProblems({})).toEqual([]);
  });
});

describe('tidySite', () => {
  it('drops blanks and empty sections but keeps unknown keys', () => {
    const out = tidySite({
      title: 'QE',
      presented_by: '',
      next_competition: { name: '', date: '2027-01-10' },
      sponsors: [{ name: ' ', url: 'x' }, { name: 'Kingston', url: '', logo_key: 'k.png' }],
      map: { rooms: [{ name: '', x: 1, y: 1 }] },
      bus_tracker: { title: '', buses: [{ name: '' }] },
      custom_flag: true,
    });
    expect(out).toEqual({ title: 'QE', sponsors: [{ name: 'Kingston', logo_key: 'k.png' }], custom_flag: true });
  });
  it('keeps populated map and bus tracker data', () => {
    const out = tidySite({
      map: { image_key: 'a.png', rooms: [{ name: 'M9', x: 10, y: 20, description: '' }] },
      bus_tracker: { embed_url: 'https://bus.example', buses: [{ name: 'Bus 1', status: 'On time', eta: '' }] },
    });
    expect(out.map).toEqual({ image_key: 'a.png', rooms: [{ name: 'M9', x: 10, y: 20 }] });
    expect(out.bus_tracker).toEqual({ embed_url: 'https://bus.example', buses: [{ name: 'Bus 1', status: 'On time' }] });
  });
});
