import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from '../lib/http';
import type {
  BatchVideoUrlsResponse,
  TeamProfile,
  TeamProfileUpdate,
  VideoProcessingStatus,
  VideoUploadAccepted,
} from '../types/api';

export function useTeams() {
  return useQuery({ queryKey: ['teams'], queryFn: () => apiFetch<TeamProfile[]>('/api/v1/teams') });
}

export function useTeam(number: string | null) {
  return useQuery({
    queryKey: ['teams', number],
    queryFn: () => apiFetch<TeamProfile>(`/api/v1/teams/${encodeURIComponent(number!)}`),
    enabled: Boolean(number),
  });
}

export function useUpdateTeam() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ number, body }: { number: string; body: TeamProfileUpdate }) =>
      apiFetch<TeamProfile>(`/api/v1/teams/${encodeURIComponent(number)}`, { method: 'PUT', body }),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ['teams'] });
      qc.invalidateQueries({ queryKey: ['teams', vars.number] });
    },
  });
}

export function useVideoStatus(number: string | null, opts?: { pollWhileProcessing?: boolean }) {
  return useQuery({
    queryKey: ['teams', number, 'video-status'],
    queryFn: () => apiFetch<VideoProcessingStatus>(`/api/v1/teams/${encodeURIComponent(number!)}/video/status`),
    enabled: Boolean(number),
    refetchInterval: (query) => {
      if (!opts?.pollWhileProcessing) return false;
      return query.state.data?.video_processing_status === 'PROCESSING' ? 3000 : false;
    },
  });
}

export function useUploadTeamVideo() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({
      number,
      file,
      keyColour,
      similarity,
      blend,
    }: {
      number: string;
      file: File;
      keyColour: string;
      similarity: number;
      blend: number;
    }) => {
      const form = new FormData();
      form.set('file', file);
      form.set('key_colour', keyColour);
      form.set('similarity', String(similarity));
      form.set('blend', String(blend));
      return apiFetch<VideoUploadAccepted>(`/api/v1/teams/${encodeURIComponent(number)}/video`, {
        method: 'POST',
        formData: form,
      });
    },
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ['teams', vars.number, 'video-status'] });
    },
  });
}

export function useBatchVideoUrls(teamNumbers: string[]) {
  const joined = teamNumbers.join(',');
  return useQuery({
    queryKey: ['teams', 'batch-videos', joined],
    queryFn: () => apiFetch<BatchVideoUrlsResponse>('/api/v1/teams/batch/videos', { query: { teams: joined } }),
    enabled: teamNumbers.length > 0,
  });
}

export function useUploadTeamLogo() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ number, file }: { number: string; file: File }) => {
      const form = new FormData();
      form.set('file', file);
      return apiFetch<TeamProfile>(`/api/v1/teams/${encodeURIComponent(number)}/logo`, { method: 'POST', formData: form });
    },
    onSuccess: (_d, vars) => {
      qc.invalidateQueries({ queryKey: ['teams'] });
      qc.invalidateQueries({ queryKey: ['teams', vars.number] });
    },
  });
}

export function useDeleteTeamLogo() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (number: string) =>
      apiFetch<TeamProfile>(`/api/v1/teams/${encodeURIComponent(number)}/logo`, { method: 'DELETE' }),
    onSuccess: (_d, number) => {
      qc.invalidateQueries({ queryKey: ['teams'] });
      qc.invalidateQueries({ queryKey: ['teams', number] });
    },
  });
}
