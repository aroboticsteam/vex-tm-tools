import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from '../lib/http';
import type { OverlayInstance, OverlayInstanceCreate, OverlayInstanceUpdate, OverlayPreview } from '../types/api';

const KEY = ['overlays'] as const;

export function useOverlays() {
  return useQuery({ queryKey: KEY, queryFn: () => apiFetch<OverlayInstance[]>('/api/v1/overlays') });
}

export function useCreateOverlay() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: OverlayInstanceCreate) => apiFetch<OverlayInstance>('/api/v1/overlays', { method: 'POST', body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
  });
}

export function useUpdateOverlay() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ entityId, body }: { entityId: string; body: OverlayInstanceUpdate }) =>
      apiFetch<OverlayInstance>(`/api/v1/overlays/${encodeURIComponent(entityId)}`, { method: 'PUT', body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
  });
}

export function useDeleteOverlay() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (entityId: string) =>
      apiFetch<void>(`/api/v1/overlays/${encodeURIComponent(entityId)}`, { method: 'DELETE' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
  });
}

/**
 * GAP (plan §5.14 / backend/routers/overlays.py's own docstring): the
 * `/preview` endpoint is a documented stub as of Wave 3b — it always
 * returns `{match: null, teams: []}` rather than the next queued match's
 * team/video data. We call the real endpoint (no fabricated data) so this
 * page reflects reality; the Overlays page surfaces that limitation in a
 * visible note rather than pretending the preview is live.
 */
export function useOverlayPreview(entityId: string | null) {
  return useQuery({
    queryKey: ['overlays', entityId, 'preview'],
    queryFn: () => apiFetch<OverlayPreview>(`/api/v1/overlays/${encodeURIComponent(entityId!)}/preview`),
    enabled: Boolean(entityId),
    refetchInterval: 10_000,
  });
}

/** Finals spotlight: show one team on the overlay (`null` returns to the match view). */
export function useSpotlightTeam() {
  return useMutation({
    mutationFn: ({ entityId, team }: { entityId: string; team: string | null }) =>
      apiFetch<void>(`/api/v1/overlays/${encodeURIComponent(entityId)}/show-team`, { method: 'POST', body: { team } }),
  });
}
