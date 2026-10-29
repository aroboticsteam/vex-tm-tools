import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from '../lib/http';

export interface UploadedImage {
  key: string;
  url: string | null;
}

export interface MatchRecordingItem {
  match_key: string;
  division: string;
  label: string;
  url: string;
  title: string | null;
}

export interface RecordingImportResult {
  saved: number;
  errors: { line: number; error: string }[];
}

/** The public overview resolves uploaded image keys to viewable URLs. */
export function usePublicOverview() {
  return useQuery({
    queryKey: ['public-site', 'overview'],
    queryFn: () =>
      apiFetch<{ map: { image_url: string } | null; sponsors: { name: string | null; logo_url: string | null }[] }>(
        '/api/v1/public/site/overview',
      ),
  });
}

export function useUploadSiteImage() {
  return useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.set('file', file);
      return apiFetch<UploadedImage>('/api/v1/site/images', { method: 'POST', formData: form });
    },
  });
}

const RECORDINGS = ['match-recordings'];

export function useMatchRecordings() {
  return useQuery({ queryKey: RECORDINGS, queryFn: () => apiFetch<MatchRecordingItem[]>('/api/v1/match-recordings') });
}

function useInvalidating<TVars, TData>(fn: (vars: TVars) => Promise<TData>) {
  const qc = useQueryClient();
  return useMutation({ mutationFn: fn, onSuccess: () => qc.invalidateQueries({ queryKey: RECORDINGS }) });
}

export const useSetMatchRecording = () =>
  useInvalidating((body: { division: string; label: string; url: string; title?: string | null }) =>
    apiFetch<MatchRecordingItem>('/api/v1/match-recordings', { method: 'PUT', body }),
  );
export const useDeleteMatchRecording = () =>
  useInvalidating((matchKey: string) =>
    apiFetch<void>(`/api/v1/match-recordings/${encodeURIComponent(matchKey)}`, { method: 'DELETE' }),
  );
export const useImportMatchRecordings = () =>
  useInvalidating((text: string) =>
    apiFetch<RecordingImportResult>('/api/v1/match-recordings/import', { method: 'POST', body: { text } }),
  );
