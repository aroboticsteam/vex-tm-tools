/** Browser Web Push helpers for the staff bell (components/layout/StaffPushToggle.tsx).
 * Team-facing subscription lives in the standalone /notify page instead. */
import { getVapidKey, subscribeStaffPush, unsubscribeStaffPush } from '../api/notifications';
import type { StaffCategory } from '../types/api';

export function pushSupported(): boolean {
  return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
}

function base64UrlToBytes(b64: string): Uint8Array<ArrayBuffer> {
  const pad = '='.repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

async function registration(): Promise<ServiceWorkerRegistration> {
  await navigator.serviceWorker.register('/sw.js');
  return navigator.serviceWorker.ready;
}

export async function currentStaffSubscription(): Promise<PushSubscription | null> {
  if (!pushSupported()) return null;
  const reg = await navigator.serviceWorker.getRegistration('/');
  return reg ? reg.pushManager.getSubscription() : null;
}

export async function enableStaffPush(categories: StaffCategory[] | null): Promise<void> {
  const permission = await Notification.requestPermission();
  if (permission !== 'granted') throw new Error('Notifications are blocked in this browser');
  const key = await getVapidKey();
  const reg = await registration();
  const sub =
    (await reg.pushManager.getSubscription()) ??
    (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: base64UrlToBytes(key) }));
  const json = sub.toJSON();
  await subscribeStaffPush({
    endpoint: sub.endpoint,
    keys: { p256dh: json.keys?.p256dh ?? '', auth: json.keys?.auth ?? '' },
    categories,
  });
}

export async function disableStaffPush(): Promise<void> {
  const sub = await currentStaffSubscription();
  if (!sub) return;
  await unsubscribeStaffPush(sub.endpoint);
  await sub.unsubscribe();
}
