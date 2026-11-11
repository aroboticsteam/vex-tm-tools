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
  ChevronDown,
  Clapperboard,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { NavLink, useLocation } from 'react-router-dom';
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

interface NavGroup {
  label: string;
  icon: typeof LayoutDashboard;
  items: NavItem[];
}

const DASHBOARD_ITEM: NavItem = { to: '/', label: 'Dashboard', icon: LayoutDashboard };

const NAV_GROUPS: NavGroup[] = [
  {
    label: 'Production',
    icon: MonitorPlay,
    items: [
      { to: '/match-control', label: 'Match Control', icon: MonitorPlay },
      { to: '/timers', label: 'Timers', icon: Timer },
      { to: '/overlays', label: 'Overlays', icon: Presentation },
      { to: '/buttons', label: 'Button Pages', icon: LayoutGrid },
      { to: '/comms', label: 'Comms', icon: Radio },
    ],
  },
  {
    label: 'Video',
    icon: Clapperboard,
    items: [
      { to: '/record', label: 'Record Clips', icon: Video },
      { to: '/analysis', label: 'Analysis', icon: Clapperboard },
    ],
  },
  {
    label: 'Event',
    icon: Users2,
    items: [
      { to: '/teams', label: 'Teams', icon: Users2 },
      { to: '/rooms', label: 'Rooms', icon: DoorOpen },
    ],
  },
  {
    label: 'Automation',
    icon: Workflow,
    items: [
      { to: '/integrations', label: 'Integrations', icon: Plug },
      { to: '/automations', label: 'Automations', icon: Workflow },
      { to: '/scripts', label: 'Scripts', icon: FileCode2 },
    ],
  },
  {
    label: 'Admin',
    icon: SettingsIcon,
    items: [
      { to: '/settings', label: 'Settings', icon: SettingsIcon },
      { to: '/users', label: 'Users & Roles', icon: ShieldCheck },
      { to: '/audit', label: 'Audit Log', icon: ScrollText },
    ],
  },
];

/** Appendix A.11 debug views — gated on `settings:edit`, same as the pages
 * themselves (AUDIT_FINDINGS.md 1.10). */
const DEBUG_NAV_ITEMS: NavItem[] = [
  { to: '/debug/integration-log', label: 'Integration Debug Log', icon: Bug },
  { to: '/debug/event-bus', label: 'Live Event Bus', icon: Activity },
];

function NavDropdown({ group }: { group: NavGroup }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const { pathname } = useLocation();
  const Icon = group.icon;
  const active = group.items.some(({ to }) => pathname === to || pathname.startsWith(`${to}/`));

  useEffect(() => setOpen(false), [pathname]);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false);
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className={`navbar-anim-link rounded-lg px-1.5 py-1.5 ${active ? 'active' : ''}`}
      >
        <Icon size={18} className="text-vmd-textMuted" />
        <span className="nav-label">{group.label}</span>
        <ChevronDown size={14} className={`ml-1 text-vmd-textMuted transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div
          role="menu"
          className="absolute left-0 top-full z-50 mt-1 flex min-w-[12rem] flex-col gap-0.5 rounded-lg border border-white/10 bg-[#1a1a1f] p-1.5 shadow-xl"
        >
          {group.items.map(({ to, label, icon: ItemIcon }) => (
            <NavLink
              key={to}
              to={to}
              role="menuitem"
              className={({ isActive }) => `navbar-anim-link rounded-md px-2 py-1.5 hover:bg-white/5 ${isActive ? 'active bg-white/10' : ''}`}
            >
              <ItemIcon size={18} className="text-vmd-textMuted" />
              <span className="nav-label">{label}</span>
            </NavLink>
          ))}
        </div>
      )}
    </div>
  );
}

export function Navbar() {
  const subject = useAuthStore((s) => s.subject);
  const clearAuth = useAuthStore((s) => s.clear);
  const canViewDebug = usePermission('settings:edit');
  const canViewAnalysis = usePermission('analysis:view');
  const groups: NavGroup[] = [
    ...NAV_GROUPS.map((g) => ({
      ...g,
      items: g.items.filter(({ to }) => to !== '/analysis' || canViewAnalysis),
    })).filter((g) => g.items.length > 0),
    ...(canViewDebug ? [{ label: 'Debug', icon: Bug, items: DEBUG_NAV_ITEMS }] : []),
  ];
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
      <div className="flex flex-1 flex-wrap items-center gap-x-3 gap-y-1">
        <NavLink
          to={DASHBOARD_ITEM.to}
          end
          className={({ isActive }) => `navbar-anim-link rounded-lg px-1.5 py-1.5 ${isActive ? 'active' : ''}`}
        >
          <DASHBOARD_ITEM.icon size={18} className="text-vmd-textMuted" />
          <span className="nav-label">{DASHBOARD_ITEM.label}</span>
        </NavLink>
        {groups.map((g) => (
          <NavDropdown key={g.label} group={g} />
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
