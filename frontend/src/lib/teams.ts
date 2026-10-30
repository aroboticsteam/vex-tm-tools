import type { TeamAlertReason, TeamAlertResult } from '../types/api';

/** Splits free text ("1234A, 5678b  9999Z") into unique upper-case team numbers. */
export function parseTeams(text: string): string[] {
  const seen: string[] = [];
  for (const raw of text.split(/[\s,;]+/)) {
    const team = raw.trim().toUpperCase();
    if (team && !seen.includes(team)) seen.push(team);
  }
  return seen;
}

export const ALERT_REASONS: Record<TeamAlertReason, string> = {
  pit_admin: 'Come to Pit Admin',
  lost_and_found: 'Come to Lost & Found',
  custom: 'Custom message',
};

/** Result summary for a toast, calling out teams nobody could reach. */
export function alertSummary(res: TeamAlertResult): { text: string; kind: 'success' | 'error' } {
  const sent = `Sent to ${res.rooms} room display${res.rooms === 1 ? '' : 's'}, ${res.pushed} phone${res.pushed === 1 ? '' : 's'}`;
  if (res.unreached_teams.length === 0) return { text: sent, kind: 'success' };
  return { text: `${sent}. Not reached: ${res.unreached_teams.join(', ')}`, kind: 'error' };
}
