import { useMutation, useQuery } from '@tanstack/react-query';
import { apiFetch } from '../lib/http';
import type {
  BroadcastRequest,
  BroadcastResult,
  NotificationStats,
  StaffCategory,
  TeamAlertRequest,
  TeamAlertResult,
} from '../types/api';

export function useNotificationStats(enabled = true) {
  return useQuery({
    queryKey: ['notification-stats'],
    queryFn: () => apiFetch<NotificationStats>('/api/v1/notifications/stats'),
    enabled,
    refetchInterval: 30_000,
  });
}

export function useBroadcast() {
  return useMutation({
    mutationFn: (body: BroadcastRequest) =>
      apiFetch<BroadcastResult>('/api/v1/notifications/broadcast', { method: 'POST', body }),
  });
}

export function useTeamAlert() {
  return useMutation({
    mutationFn: (body: TeamAlertRequest) =>
      apiFetch<TeamAlertResult>('/api/v1/notifications/team-alert', { method: 'POST', body }),
  });
}

export function useTestPush() {
  return useMutation({
    mutationFn: () => apiFetch<BroadcastResult>('/api/v1/notifications/test', { method: 'POST' }),
  });
}

// ── Staff Web Push registration ──────────────────────────────────────────

export async function getVapidKey(): Promise<string> {
  const res = await apiFetch<{ public_key: string }>('/api/v1/public/push/vapid-key');
  return res.public_key;
}

export function subscribeStaffPush(body: {
  endpoint: string;
  keys: { p256dh: string; auth: string };
  categories: StaffCategory[] | null;
}) {
  return apiFetch<void>('/api/v1/notifications/staff/subscribe', { method: 'POST', body });
}

export function unsubscribeStaffPush(endpoint: string) {
  return apiFetch<void>('/api/v1/notifications/staff/unsubscribe', { method: 'POST', body: { endpoint } });
}
