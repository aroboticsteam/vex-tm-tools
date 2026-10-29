/** Pure helpers for the in-browser clip recorder (issues #1/#12). */

export const MIN_CLIP_SECONDS = 5;
export const MAX_CLIP_SECONDS = 10;
export const DEFAULT_CLIP_SECONDS = 8;
export const OUTPUT_SIZE = 1080;
export const TARGET_FPS = 60;

const MIME_CANDIDATES = ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm', 'video/mp4'];

/** First container/codec the browser's MediaRecorder supports (Safari only does mp4). */
export function pickMimeType(isSupported: (mime: string) => boolean): string | null {
  return MIME_CANDIDATES.find((m) => isSupported(m)) ?? null;
}

export function extensionFor(mime: string): 'webm' | 'mp4' {
  return mime.startsWith('video/mp4') ? 'mp4' : 'webm';
}

export function clampSeconds(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_CLIP_SECONDS;
  return Math.max(MIN_CLIP_SECONDS, Math.min(MAX_CLIP_SECONDS, Math.round(value)));
}

/** Largest centred square inside a `width` x `height` frame, as a source rect. */
export function squareCrop(width: number, height: number): { sx: number; sy: number; size: number } {
  const size = Math.max(0, Math.min(width, height));
  return { sx: Math.floor((width - size) / 2), sy: Math.floor((height - size) / 2), size };
}

export function isValidTeamNumber(value: string): boolean {
  return /^[A-Za-z0-9]{1,20}$/.test(value.trim());
}
