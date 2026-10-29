import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from '../lib/http';
import type {
  AgendaItem,
  CommsPreset,
  CommsScreen,
  Station,
  StationMessage,
  StationRole,
} from '../types/api';

const BASE = '/api/v1/comms';

function useList<T>(key: string, path: string, refetchInterval?: number) {
  return useQuery({ queryKey: ['comms', key], queryFn: () => apiFetch<T[]>(`${BASE}/${path}`), refetchInterval });
}

function useInvalidating<TVars, TData>(key: string, fn: (vars: TVars) => Promise<TData>) {
  const qc = useQueryClient();
  return useMutation({ mutationFn: fn, onSuccess: () => qc.invalidateQueries({ queryKey: ['comms', key] }) });
}

export const useStations = () => useList<Station>('stations', 'stations');
export const usePresets = () => useList<CommsPreset>('presets', 'presets');
export const useAgenda = () => useList<AgendaItem>('agenda', 'agenda');
/** Polled: the desk should see iPad messages within a few seconds. */
export const useInbox = () => useList<StationMessage>('inbox', 'inbox?unacknowledged=true', 5000);

export const useSaveStation = () =>
  useInvalidating('stations', ({ slug, body, isNew }: { slug: string; body: { name: string; role: StationRole }; isNew: boolean }) =>
    isNew
      ? apiFetch<Station>(`${BASE}/stations`, { method: 'POST', body: { slug, ...body } })
      : apiFetch<Station>(`${BASE}/stations/${slug}`, { method: 'PUT', body }),
  );
export const useDeleteStation = () =>
  useInvalidating('stations', (slug: string) => apiFetch<void>(`${BASE}/stations/${slug}`, { method: 'DELETE' }));

type PresetBody = Omit<CommsPreset, 'id'>;
export const useSavePreset = () =>
  useInvalidating('presets', ({ id, body }: { id?: string; body: PresetBody }) =>
    id
      ? apiFetch<CommsPreset>(`${BASE}/presets/${id}`, { method: 'PUT', body })
      : apiFetch<CommsPreset>(`${BASE}/presets`, { method: 'POST', body }),
  );
export const useDeletePreset = () =>
  useInvalidating('presets', (id: string) => apiFetch<void>(`${BASE}/presets/${id}`, { method: 'DELETE' }));

type AgendaBody = Omit<AgendaItem, 'id'>;
export const useSaveAgendaItem = () =>
  useInvalidating('agenda', ({ id, body }: { id?: string; body: AgendaBody }) =>
    id
      ? apiFetch<AgendaItem>(`${BASE}/agenda/${id}`, { method: 'PUT', body })
      : apiFetch<AgendaItem>(`${BASE}/agenda`, { method: 'POST', body }),
  );
export const useDeleteAgendaItem = () =>
  useInvalidating('agenda', (id: string) => apiFetch<void>(`${BASE}/agenda/${id}`, { method: 'DELETE' }));

export interface Audience {
  stations?: string[];
  roles?: StationRole[];
}

export function useSendMessage() {
  return useMutation({
    mutationFn: (body: Audience & { text: string; flash: boolean }) =>
      apiFetch<{ stations: string[] }>(`${BASE}/send`, { method: 'POST', body }),
  });
}

export function usePushScreen() {
  return useMutation({
    mutationFn: (body: Audience & { screen: CommsScreen }) =>
      apiFetch<{ stations: string[] }>(`${BASE}/screen`, { method: 'POST', body }),
  });
}

export const useAckMessage = () =>
  useInvalidating('inbox', (id: string) => apiFetch<void>(`${BASE}/inbox/${id}/ack`, { method: 'POST' }));
