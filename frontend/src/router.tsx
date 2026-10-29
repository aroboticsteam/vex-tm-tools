import { createBrowserRouter, Navigate } from 'react-router-dom';
import { ProtectedLayout } from './components/layout/ProtectedLayout';
import { AdminLoginPage } from './pages/AdminLogin';
import { SpotifyCallbackPage } from './pages/SpotifyCallback';
import { DashboardPage } from './pages/Dashboard';
import { MatchControlPage } from './pages/MatchControl';
import { IntegrationsPage } from './pages/Integrations';
import { AutomationsPage } from './pages/Automations';
import { ScriptsPage } from './pages/Scripts';
import { TimersPage } from './pages/Timers';
import { OverlaysPage } from './pages/Overlays';
import { TeamsPage } from './pages/Teams';
import { TeamDetailPage } from './pages/TeamDetail';
import { RoomsPage } from './pages/Rooms';
import { ButtonPagesPage } from './pages/ButtonPages';
import { RecorderPage } from './pages/Recorder';
import { AuditLogPage } from './pages/AuditLog';
import { SettingsPage } from './pages/Settings';
import { UsersPage } from './pages/Users';
import { DebugIntegrationLogPage } from './pages/DebugIntegrationLog';
import { DebugEventBusPage } from './pages/DebugEventBus';

// createBrowserRouter with nested routes (plan Appendix B.2). The
// /prompter/<entity_id> and /overlay/<entity_id> pages are explicitly out
// of this SPA's scope (Appendix B.3 — standalone HTML+vanilla JS served
// directly by FastAPI) so they are NOT registered here; the backend
// handles those paths itself.
export const router = createBrowserRouter([
  { path: '/admin_login', element: <AdminLoginPage /> },
  { path: '/integrations/spotify/callback', element: <SpotifyCallbackPage /> },
  {
    path: '/',
    element: <ProtectedLayout />,
    children: [
      { index: true, element: <DashboardPage /> },
      // Field Monitor was merged into Match Control; keep old links working.
      { path: 'fields', element: <Navigate to="/match-control" replace /> },
      { path: 'match-control', element: <MatchControlPage /> },
      { path: 'integrations', element: <IntegrationsPage /> },
      { path: 'automations', element: <AutomationsPage /> },
      { path: 'scripts', element: <ScriptsPage /> },
      { path: 'timers', element: <TimersPage /> },
      { path: 'overlays', element: <OverlaysPage /> },
      { path: 'teams', element: <TeamsPage /> },
      { path: 'teams/:number', element: <TeamDetailPage /> },
      { path: 'rooms', element: <RoomsPage /> },
      { path: 'buttons', element: <ButtonPagesPage /> },
      { path: 'record', element: <RecorderPage /> },
      { path: 'audit', element: <AuditLogPage /> },
      { path: 'settings', element: <SettingsPage /> },
      { path: 'users', element: <UsersPage /> },
      { path: 'debug/integration-log', element: <DebugIntegrationLogPage /> },
      { path: 'debug/event-bus', element: <DebugEventBusPage /> },
    ],
  },
]);
