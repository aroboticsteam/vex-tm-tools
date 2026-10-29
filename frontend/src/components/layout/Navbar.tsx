import {
  LayoutDashboard,
  MonitorPlay,
  Plug,
  Workflow,
  FileCode2,
  Timer,
  Presentation,
  Users2,
  ScrollText,
  Settings as SettingsIcon,
  ShieldCheck,
  LayoutGrid,
  Video,
  Radio,
  LogOut,
  Bug,
  Activity,
  DoorOpen,
} from 'lucide-react';
import { NavLink } from 'react-router-dom';
import { useMutation } from '@tanstack/react-query';
import { logout } from '../../api/auth';
import { useAuthStore } from '../../stores/auth';
import { usePermission } from '../../hooks/usePermission';
import { StaffPushToggle } from './StaffPushToggle';

interface NavItem {
  to: string;
  label: string;
  icon: typeof LayoutDashboard;
}

const NAV_ITEMS: NavItem[] = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/match-control', label: 'Match Control', icon: MonitorPlay },
  { to: '/integrations', label: 'Integrations', icon: Plug },
  { to: '/automations', label: 'Automations', icon: Workflow },
  { to: '/scripts', label: 'Scripts', icon: FileCode2 },
  { to: '/timers', label: 'Timers', icon: Timer },
  { to: '/overlays', label: 'Overlays', icon: Presentation },
  { to: '/teams', label: 'Teams', icon: Users2 },
  { to: '/record', label: 'Record Clips', icon: Video },
  { to: '/rooms', label: 'Rooms', icon: DoorOpen },
  { to: '/comms', label: 'Comms', icon: Radio },
  { to: '/buttons', label: 'Button Pages', icon: LayoutGrid },
  { to: '/audit', label: 'Audit Log', icon: ScrollText },
  { to: '/settings', label: 'Settings', icon: SettingsIcon },
  { to: '/users', label: 'Users & Roles', icon: ShieldCheck },
];

/** Appendix A.11 debug views — gated on `settings:edit`, same as the pages
 * themselves (AUDIT_FINDINGS.md 1.10). */
const DEBUG_NAV_ITEMS: NavItem[] = [
  { to: '/debug/integration-log', label: 'Integration Debug Log', icon: Bug },
  { to: '/debug/event-bus', label: 'Live Event Bus', icon: Activity },
];

export function Navbar() {
  const subject = useAuthStore((s) => s.subject);
  const clearAuth = useAuthStore((s) => s.clear);
  const canViewDebug = usePermission('settings:edit');
  const logoutMutation = useMutation({
    mutationFn: logout,
    onSuccess: () => {
      clearAuth();
      window.location.href = '/admin_login';
    },
  });

  return (
    <nav className="vmd-navbar mx-4 mt-4 flex flex-wrap items-center gap-1 lg:mx-8">
      <span className="mr-4 text-sm font-semibold tracking-wide text-vmd-textStrong">TMTools</span>
      <div className="flex flex-1 flex-wrap gap-1">
        {NAV_ITEMS.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            className={({ isActive }) => `navbar-anim-link rounded-lg px-1.5 py-1.5 ${isActive ? 'active' : ''}`}
          >
            <Icon size={18} className="text-vmd-textMuted" />
            <span className="nav-label">{label}</span>
          </NavLink>
        ))}
        {canViewDebug &&
          DEBUG_NAV_ITEMS.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) => `navbar-anim-link rounded-lg px-1.5 py-1.5 ${isActive ? 'active' : ''}`}
            >
              <Icon size={18} className="text-vmd-textMuted" />
              <span className="nav-label">{label}</span>
            </NavLink>
          ))}
      </div>
      <div className="ml-auto flex items-center gap-2 pl-2 text-xs text-vmd-textSubtle">
        <StaffPushToggle />
        {subject && <span>{subject}</span>}
        <button
          onClick={() => logoutMutation.mutate()}
          className="navbar-anim-link rounded-lg px-1.5 py-1.5"
          title="Log out"
        >
          <LogOut size={18} className="text-vmd-textMuted" />
          <span className="nav-label">Log out</span>
        </button>
      </div>
    </nav>
  );
}
