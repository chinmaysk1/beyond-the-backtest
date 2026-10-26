'use client';

import { useEffect, useState } from 'react';

/* "Connect your AI": the user's own model writes and tests strategies on their
 * account through the site's MCP server (/api/mcp). We run no model. Claude
 * and ChatGPT connectors sign in with OAuth; Claude Code, or anything else that
 * takes a bearer token, uses a personal access token created here. The
 * copy-prompt is the fallback for a chatbot with no tools: it gets the same
 * reference text the MCP server serves. */

type Token = { id: string; name: string | null; createdAt: string; lastUsedAt: string | null };
type App = { clientId: string; name: string | null; since: string };
type Client = 'claude' | 'chatgpt' | 'code';

export default function ConnectAI({ prompt, placeholder }: { prompt: string; placeholder: string }) {
  const [origin, setOrigin] = useState('');
  const [client, setClient] = useState<Client>('claude');
  const [tokens, setTokens] = useState<Token[] | null>(null);
  const [apps, setApps] = useState<App[]>([]);
  const [fresh, setFresh] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  useEffect(() => { setOrigin(window.location.origin); }, []);
  useEffect(() => { loadTokens(); }, []);

  const url = `${origin}/api/mcp`;
  const local = /^https?:\/\/(localhost|127\.|\[::1\])/.test(origin);

  async function loadTokens() {
    try {
      const res = await fetch('/api/tokens');
      if (res.ok) {
        const out = await res.json();
        setTokens(out.tokens ?? []);
        setApps(out.apps ?? []);
      }
    } catch { /* the list is a convenience */ }
  }

  async function create() {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch('/api/tokens', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: client === 'code' ? 'Claude Code' : 'Access token' }),
      });
      const out = await res.json();
      if (!res.ok) throw new Error(out.error ?? 'Could not create a token');
      setFresh(out.token);
      loadTokens();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function revoke(id: string) {
    await fetch(`/api/tokens?id=${encodeURIComponent(id)}`, { method: 'DELETE' }).catch(() => {});
    if (fresh) setFresh(null);
    loadTokens();
  }

  async function disconnect(clientId: string) {
    await fetch(`/api/tokens?client=${encodeURIComponent(clientId)}`, { method: 'DELETE' }).catch(() => {});
    loadTokens();
  }

  async function copy(key: string, text: string) {
    try { await navigator.clipboard.writeText(text); } catch { /* denied */ }
    setCopied(key);
    setTimeout(() => setCopied((c) => (c === key ? null : c)), 1600);
  }

  const command = `claude mcp add --transport http btb ${url}`
    + (fresh ? ` --header "Authorization: Bearer ${fresh}"` : '');

  return (
    <div className="sv-custom sv-connect">
      <p className="sv-connect-lede">
        Let the AI you already use write and test strategies on your account. It can check Pine,
        run tests and read their scores. It can’t see anyone else’s tests, and we never see your chats.
      </p>

      <div className="sv-url">
        <span className="mono">{url || '…/api/mcp'}</span>
        <button type="button" className={copied === 'url' ? 'ok' : ''} onClick={() => copy('url', url)}>
          {copied === 'url' ? 'Copied' : 'Copy'}
        </button>
      </div>
      {local && (
        <div className="sv-warnbox">
          This address is only reachable from this computer. Claude Code works from here; Claude.ai
          and ChatGPT connect from their own servers and need the site’s public address.
        </div>
      )}

      <div className="sv-connect-tabs">
        {([['claude', 'Claude'], ['chatgpt', 'ChatGPT'], ['code', 'Claude Code']] as [Client, string][]).map(([id, label]) => (
          <button key={id} type="button" className={client === id ? 'on' : ''} onClick={() => setClient(id)}>{label}</button>
        ))}
      </div>

      {client === 'claude' && (
        <ol className="sv-connect-steps">
          <li>In Claude, open <b>Settings → Connectors</b> and choose <b>Add custom connector</b>.</li>
          <li>Paste the address above. Claude opens this site to sign in; approve the request.</li>
          <li>In a chat, ask for a test: <i>“test a breakout strategy on BTC 4h with a 2 ATR stop”</i>.</li>
        </ol>
      )}
      {client === 'chatgpt' && (
        <ol className="sv-connect-steps">
          <li>In ChatGPT’s settings, turn on developer mode and add a custom <b>MCP connector</b>.</li>
          <li>Paste the address above and choose OAuth. ChatGPT opens this site to sign in; approve it.</li>
          <li>In a chat with the connector on, describe the strategy you want tested.</li>
        </ol>
      )}
      {client === 'code' && (
        <>
          <ol className="sv-connect-steps">
            <li>Create an access token below. It is shown once.</li>
            <li>Run this in a terminal:</li>
          </ol>
          <div className="sv-url">
            <span className="mono">{command}</span>
            <button type="button" className={copied === 'cmd' ? 'ok' : ''} onClick={() => copy('cmd', command)}>
              {copied === 'cmd' ? 'Copied' : 'Copy'}
            </button>
          </div>
          <div className="sv-note">Or leave the header off and run <span className="mono">/mcp</span> in Claude Code to sign in with OAuth.</div>
        </>
      )}

      <div className="sv-tokens">
        <div className="sv-tokens-head">
          <b>Access tokens</b>
          <button type="button" className="sv-link" disabled={busy} onClick={create}>{busy ? 'Creating…' : 'Create access token'}</button>
        </div>
        {fresh && (
          <div className="sv-url sv-fresh">
            <span className="mono">{fresh}</span>
            <button type="button" className={copied === 'tok' ? 'ok' : ''} onClick={() => copy('tok', fresh)}>
              {copied === 'tok' ? 'Copied' : 'Copy'}
            </button>
          </div>
        )}
        {fresh && <div className="sv-note">Copy it now: it won’t be shown again. Anyone with it can run tests as you.</div>}
        {error && <div className="sv-error">{error}</div>}
        {tokens && tokens.length > 0 && (
          <ul className="sv-token-list">
            {tokens.map((t) => (
              <li key={t.id}>
                <span>{t.name ?? 'Access token'}</span>
                <small>created {new Date(t.createdAt).toLocaleDateString()}
                  {' · '}{t.lastUsedAt ? `last used ${new Date(t.lastUsedAt).toLocaleDateString()}` : 'never used'}</small>
                <button type="button" className="sv-link" onClick={() => revoke(t.id)}>Revoke</button>
              </li>
            ))}
          </ul>
        )}
        {tokens && tokens.length === 0 && !fresh && <div className="sv-note">No tokens yet. Connectors that sign in don’t need one.</div>}
        {apps.length > 0 && (
          <>
            <div className="sv-tokens-head" style={{ marginTop: 12 }}><b>Connected apps</b></div>
            <ul className="sv-token-list">
              {apps.map((a) => (
                <li key={a.clientId}>
                  <span>{a.name ?? 'An AI app'}</span>
                  <small>connected {new Date(a.since).toLocaleDateString()}</small>
                  <button type="button" className="sv-link" onClick={() => disconnect(a.clientId)}>Disconnect</button>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>

      <div className="sv-connect-fallback">
        <span>No AI with tools? Copy a prompt into any chatbot with your idea, then paste the Pine it gives you into <b>Your Pine</b>.</span>
        <button type="button" className={`sv-link ${copied === 'prompt' ? 'ok' : ''}`}
                onClick={() => copy('prompt', prompt + '<' + placeholder + '>')}>
          {copied === 'prompt' ? 'Prompt copied' : 'Copy a prompt'}
        </button>
      </div>
    </div>
  );
}
