import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from '../lib/http';
import type { ClipKind, TeamClips, VideoUploadAccepted } from '../types/api';

export function useTeamClips(number: string | null) {
  return useQuery({
    queryKey: ['teams', number, 'clips'],
    queryFn: () => apiFetch<TeamClips>(`/api/v1/teams/${encodeURIComponent(number!)}/clips`),
    enabled: !!number,
    // Keying runs in the background after upload: poll until both clips settle.
    refetchInterval: (query) => {
      const clips = query.state.data?.clips;
      return clips && Object.values(clips).some((c) => c.status === 'PROCESSING') ? 2000 : false;
    },
  });
}

export function useUploadClip() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({
      number,
      kind,
      file,
      keyColour,
      similarity,
      blend,
    }: {
      number: string;
      kind: ClipKind;
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
      return apiFetch<VideoUploadAccepted>(
        `/api/v1/teams/${encodeURIComponent(number)}/clips/${kind}`,
        { method: 'POST', formData: form },
      );
    },
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ['teams', vars.number, 'clips'] });
    },
  });
}
