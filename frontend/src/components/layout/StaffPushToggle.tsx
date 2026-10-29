import { useEffect, useState } from 'react';
import { Bell, BellOff } from 'lucide-react';
import { usePermission } from '../../hooks/usePermission';
import { currentStaffSubscription, disableStaffPush, enableStaffPush, pushSupported } from '../../lib/push';
import { useUiStore } from '../../stores/ui';

/** Navbar bell: lets a staff member (permission `notifications:receive`) turn
 * Web Push alerts on or off for this browser — system alerts (integration
 * degraded, leader failover, ...) and staff broadcasts. */
export function StaffPushToggle() {
  const allowed = usePermission('notifications:receive');
  const pushToast = useUiStore((s) => s.pushToast);
  const [enabled, setEnabled] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!allowed || !pushSupported()) return;
    currentStaffSubscription()
      .then((sub) => setEnabled(sub !== null && Notification.permission === 'granted'))
      .catch(() => setEnabled(false));
  }, [allowed]);

  if (!allowed || !pushSupported()) return null;

  async function toggle() {
    setBusy(true);
    try {
      if (enabled) {
        await disableStaffPush();
        setEnabled(false);
        pushToast('Push alerts turned off for this browser', 'info');
      } else {
        await enableStaffPush(null);
        setEnabled(true);
        pushToast('Push alerts turned on for this browser', 'success');
      }
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Could not change push alerts', 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <button
      onClick={toggle}
      disabled={busy}
      className="navbar-anim-link rounded-lg px-1.5 py-1.5 disabled:opacity-40"
      title={enabled ? 'Turn off push alerts' : 'Turn on push alerts'}
    >
      {enabled ? <Bell size={18} className="text-vmd-success" /> : <BellOff size={18} className="text-vmd-textMuted" />}
      <span className="nav-label">{enabled ? 'Alerts on' : 'Alerts off'}</span>
    </button>
  );
}
