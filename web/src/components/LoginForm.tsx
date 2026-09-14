'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import Brand from './Brand';

export default function LoginForm() {
  const router = useRouter();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(body.error ?? 'Sign-in failed');
        setBusy(false);
        return;
      }
      // refresh() re-runs the server component, which re-reads the session
      // cookie the response just set.
      router.replace('/');
      router.refresh();
    } catch {
      setError('Could not reach the server');
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <div className="login-card">
        <Brand />
        <h1>Sign in</h1>

        <form onSubmit={submit}>
          <div className="field">
            <label htmlFor="u">Username</label>
            <input
              id="u"
              name="username"
              autoComplete="username"
              spellCheck={false}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <label htmlFor="p">Password</label>
            <input
              id="p"
              name="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          <button className="btn-primary" type="submit" disabled={busy}>
            {busy ? 'Checking…' : 'Sign in'}
          </button>
        </form>

        {error && <p className="login-error">{error}</p>}
      </div>
    </div>
  );
}
