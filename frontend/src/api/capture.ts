import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from '../lib/http';
import type { CaptureRecording, CaptureStatus, ReplayAction, ReplayState } from '../types/api';

const STATUS = ['capture', 'status'] as const;
const RECORDINGS = ['capture', 'recordings'] as const;

/** Polled rather than socket-driven: the deck is open for the whole event and
 * a 2 s refresh is plenty for "is the clip uploaded yet". */
export function useCaptureStatus() {
  return useQuery({
    queryKey: STATUS,
    queryFn: () => apiFetch<CaptureStatus>('/api/v1/capture/status'),
    refetchInterval: 2000,
  });
}

export function useCaptureRecordings(params: { team?: string; matchType?: string; limit?: number } = {}) {
  const qs = new URLSearchParams();
  if (params.team) qs.set('team', params.team);
  if (params.matchType) qs.set('match_type', params.matchType);
  if (params.limit) qs.set('limit', String(params.limit));
  const query = qs.toString();
  return useQuery({
    queryKey: [...RECORDINGS, query],
    queryFn: () => apiFetch<CaptureRecording[]>(`/api/v1/capture/recordings${query ? `?${query}` : ''}`),
    refetchInterval: 3000,
  });
}

function useInvalidateCapture() {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: ['capture'] });
}

export function useMarkMoment() {
  const invalidate = useInvalidateCapture();
  return useMutation({
    mutationFn: (length_s: number) => apiFetch<unknown>('/api/v1/capture/moments', { method: 'POST', body: { length_s } }),
    onSuccess: invalidate,
  });
}

export function useStopRecording() {
  const invalidate = useInvalidateCapture();
  return useMutation({
    mutationFn: () => apiFetch<unknown>('/api/v1/capture/recordings/stop', { method: 'POST' }),
    onSuccess: invalidate,
  });
}

export function useReplayState(replayId: string) {
  return useQuery({
    queryKey: ['capture', 'replay', replayId],
    queryFn: () => apiFetch<ReplayState>(`/api/v1/capture/replay/${encodeURIComponent(replayId)}`),
    refetchInterval: 2000,
  });
}

export function useReplayPlay(replayId: string) {
  const invalidate = useInvalidateCapture();
  return useMutation({
    mutationFn: (body: { kind: 'moment' | 'recording'; id: string; rate?: number; loop?: boolean }) =>
      apiFetch<unknown>(`/api/v1/capture/replay/${encodeURIComponent(replayId)}/play`, { method: 'POST', body }),
    onSuccess: invalidate,
  });
}

export function useReplayControl(replayId: string) {
  const invalidate = useInvalidateCapture();
  return useMutation({
    mutationFn: (body: { action: ReplayAction; value?: number }) =>
      apiFetch<unknown>(`/api/v1/capture/replay/${encodeURIComponent(replayId)}/control`, { method: 'POST', body }),
    onSuccess: invalidate,
  });
}
