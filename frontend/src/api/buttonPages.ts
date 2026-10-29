import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from '../lib/http';
import type { ButtonInput, ButtonItem, ButtonPage, PressResult } from '../types/api';

const KEY = ['button-pages'];

export function useButtonPages() {
  return useQuery({ queryKey: KEY, queryFn: () => apiFetch<ButtonPage[]>('/api/v1/button-pages') });
}

function useInvalidating<TVars, TData>(fn: (vars: TVars) => Promise<TData>) {
  const qc = useQueryClient();
  return useMutation({ mutationFn: fn, onSuccess: () => qc.invalidateQueries({ queryKey: KEY }) });
}

export function useCreatePage() {
  return useInvalidating((body: { name: string; columns: number; position: number }) =>
    apiFetch<ButtonPage>('/api/v1/button-pages', { method: 'POST', body }),
  );
}

export function useUpdatePage() {
  return useInvalidating(({ id, body }: { id: string; body: { name?: string; columns?: number; position?: number } }) =>
    apiFetch<ButtonPage>(`/api/v1/button-pages/${id}`, { method: 'PUT', body }),
  );
}

export function useDeletePage() {
  return useInvalidating((id: string) => apiFetch<void>(`/api/v1/button-pages/${id}`, { method: 'DELETE' }));
}

export function useCreateButton() {
  return useInvalidating(({ pageId, body }: { pageId: string; body: ButtonInput }) =>
    apiFetch<ButtonItem>(`/api/v1/button-pages/${pageId}/buttons`, { method: 'POST', body }),
  );
}

export function useUpdateButton() {
  return useInvalidating(({ id, body }: { id: string; body: Partial<ButtonInput> }) =>
    apiFetch<ButtonItem>(`/api/v1/button-pages/buttons/${id}`, { method: 'PUT', body }),
  );
}

export function useDeleteButton() {
  return useInvalidating((id: string) => apiFetch<void>(`/api/v1/button-pages/buttons/${id}`, { method: 'DELETE' }));
}

export function usePressButton() {
  return useMutation({
    mutationFn: (id: string) => apiFetch<PressResult>(`/api/v1/button-pages/buttons/${id}/press`, { method: 'POST' }),
  });
}
