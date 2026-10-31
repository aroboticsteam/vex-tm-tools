// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

interface Hooks {
  handle: (msg: unknown) => void;
  video: HTMLVideoElement;
}

const html = readFileSync(resolve(__dirname, '../../../backend/static/replay.html'), 'utf-8');
const script = html.slice(html.lastIndexOf('<script>') + 8, html.lastIndexOf('</script>'));

function load(): Hooks {
  document.body.innerHTML = '<div id="stage"><video id="player"></video><div id="label"></div></div>';
  const video = document.getElementById('player') as HTMLVideoElement;
  video.play = vi.fn().mockResolvedValue(undefined);
  video.pause = vi.fn();
  video.load = vi.fn();
  class FakeSocket {
    onopen = null;
    onmessage = null;
    onclose = null;
  }
  vi.stubGlobal('WebSocket', FakeSocket);
  window.history.pushState({}, '', '/replay/main?label=1');
  // eslint-disable-next-line no-new-func
  new Function(script)();
  return (window as unknown as { __replayTestHooks: Hooks }).__replayTestHooks;
}

describe('replay.html player', () => {
  let hooks: Hooks;
  beforeEach(() => {
    hooks = load();
  });

  it('plays a url at the requested rate, shows the caption, and ignores play without url', () => {
    hooks.handle({ type: 'play' });
    expect(hooks.video.classList.contains('on')).toBe(false);
    hooks.handle({ type: 'play', url: 'http://s3/x.mp4', label: 'Q 7 · moment 1', rate: 0.25, loop: true });
    expect(hooks.video.getAttribute('src')).toBe('http://s3/x.mp4');
    expect(hooks.video.playbackRate).toBe(0.25);
    expect(hooks.video.loop).toBe(true);
    expect(hooks.video.classList.contains('on')).toBe(true);
    expect(document.getElementById('label')?.textContent).toBe('Q 7 · moment 1');
    expect(hooks.video.play).toHaveBeenCalled();
  });

  it('starts paused when told to, and handles pause/resume/rate/seek/restart', () => {
    hooks.handle({ type: 'play', url: 'http://s3/x.mp4', paused: true });
    expect(hooks.video.play).not.toHaveBeenCalled();
    hooks.handle({ type: 'resume' });
    expect(hooks.video.play).toHaveBeenCalledTimes(1);
    hooks.handle({ type: 'pause' });
    expect(hooks.video.pause).toHaveBeenCalled();
    hooks.handle({ type: 'rate', value: 0.75 });
    expect(hooks.video.playbackRate).toBe(0.75);
    hooks.handle({ type: 'seek', value: 4 });
    expect(hooks.video.currentTime).toBe(4);
    hooks.handle({ type: 'restart' });
    expect(hooks.video.currentTime).toBe(0);
  });

  it('stop hides the video and ignores junk messages', () => {
    hooks.handle({ type: 'play', url: 'http://s3/x.mp4' });
    hooks.handle(null);
    hooks.handle({ type: 'nonsense' });
    hooks.handle({ type: 'stop' });
    expect(hooks.video.classList.contains('on')).toBe(false);
    expect(document.getElementById('label')?.classList.contains('on')).toBe(false);
  });
});
