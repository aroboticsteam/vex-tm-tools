import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { adminLogin } from '../api/auth';
import { Button } from '../components/ui/Button';
import { Input, Label } from '../components/ui/Input';
import { Card } from '../components/ui/Card';

/** Plan §3.9/§11: emergency `admin_local` login, outside the OIDC flow.
 * Username is always `admin_local`; password is whatever the deployer set
 * via `ADMIN_LOCAL_PASSWORD`. On success the backend sets a signed session
 * cookie, so the app just needs to re-run its `whoami` query. */
export function AdminLoginPage() {
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () => adminLogin('admin_local', password),
    onSuccess: () => {
      window.location.href = '/';
    },
    onError: (err: Error) => setError(err.message),
  });

  return (
    <div className="flex min-h-screen items-center justify-center bg-vmd-bg px-4">
      <Card className="w-full max-w-sm">
        <h1 className="mb-1 text-xl font-semibold text-vmd-textStrong">Emergency Admin Login</h1>
        <p className="mb-5 text-sm text-vmd-textMuted">
          Break-glass access for <span className="font-mono">admin_local</span>. Prefer{' '}
          <a href="/auth/login" className="text-vmd-link underline">
            signing in via OIDC
          </a>{' '}
          when it's available.
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setError(null);
            mutation.mutate();
          }}
          className="space-y-3"
        >
          <div>
            <Label>Username</Label>
            <Input value="admin_local" readOnly disabled />
          </div>
          <div>
            <Label>Password</Label>
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              aria-label="Password"
              autoFocus
            />
          </div>
          {error && <p className="text-sm text-vmd-danger">{error}</p>}
          <Button type="submit" variant="primary" className="w-full justify-center" disabled={mutation.isPending}>
            {mutation.isPending ? 'Signing in…' : 'Log in'}
          </Button>
        </form>
      </Card>
    </div>
  );
}
