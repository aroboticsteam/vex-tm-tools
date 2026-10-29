import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from '../lib/http';
import type { Room, RoomInput } from '../types/api';

export function useRooms() {
  return useQuery({ queryKey: ['rooms'], queryFn: () => apiFetch<Room[]>('/api/v1/rooms') });
}

export function useCreateRoom() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: RoomInput & { slug: string }) => apiFetch<Room>('/api/v1/rooms', { method: 'POST', body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['rooms'] }),
  });
}

export function useUpdateRoom() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ slug, body }: { slug: string; body: RoomInput }) =>
      apiFetch<Room>(`/api/v1/rooms/${encodeURIComponent(slug)}`, { method: 'PUT', body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['rooms'] }),
  });
}

export function useDeleteRoom() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (slug: string) => apiFetch<void>(`/api/v1/rooms/${encodeURIComponent(slug)}`, { method: 'DELETE' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['rooms'] }),
  });
}
