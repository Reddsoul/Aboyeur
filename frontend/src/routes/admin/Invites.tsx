import { useEffect, useState } from 'react';
import './Admin.css';
import * as api from '../../api';
import { dateTime, relativeTime } from './format';

export default function Invites() {
  const [data, setData] = useState<{ invites: api.InviteCode[]; signup_mode: 'invite' | 'open' } | null>(null);
  const [form, setForm] = useState({ uses: '1', days: '30', note: '' });
  const [msg, setMsg] = useState<string | null>(null);

  const load = () => api.adminInvites().then(setData).catch(() => setMsg("Couldn't load invites."));
  useEffect(() => {
    void load();
  }, []);

  async function create() {
    setMsg(null);
    try {
      const inv = await api.adminCreateInvite({
        max_uses: form.uses ? Number(form.uses) : null,
        expires_days: form.days ? Number(form.days) : null,
        note: form.note || undefined,
      });
      await load();
      setMsg(`Created: ${window.location.origin}/signup?invite=${inv.code}`);
    } catch (err) {
      setMsg(err instanceof api.ApiError ? err.message : 'Failed.');
    }
  }

  return (
    <div className="ad-page">
      <div className="ad-inner">
        <div className="ad-head">
          <div>
            <h1 className="ad-title">Invites</h1>
            <p className="ad-sub">
              Signup is <b>{data?.signup_mode ?? '…'}</b> (SIGNUP_MODE in .env). In invite mode the form
              needs one of these codes; in open mode they're ignored.
            </p>
          </div>
        </div>

        <div className="ad-card">
          <div className="ad-inline-form">
            <span className="ad-cell-muted">uses</span>
            <input className="text-input ad-input-sm" placeholder="∞" value={form.uses} onChange={(e) => setForm((f) => ({ ...f, uses: e.target.value }))} />
            <span className="ad-cell-muted">expires in days</span>
            <input className="text-input ad-input-sm" placeholder="never" value={form.days} onChange={(e) => setForm((f) => ({ ...f, days: e.target.value }))} />
            <input className="text-input" placeholder="note (who it's for)" value={form.note} onChange={(e) => setForm((f) => ({ ...f, note: e.target.value }))} style={{ width: 240 }} />
            <button type="button" className="btn-primary" onClick={() => void create()}>New invite</button>
          </div>
          {msg && <p className="ad-sub ad-code" style={{ marginTop: 8, userSelect: 'all' }}>{msg}</p>}
        </div>

        {!data ? (
          <p className="ad-sub">Loading…</p>
        ) : data.invites.length === 0 ? (
          <div className="ad-empty"><h2>No invites</h2><p>Mint one above, or `aboyeur invite` from the CLI.</p></div>
        ) : (
          <div className="ad-card ad-card-pad0">
            <div className="ad-table-wrap">
              <table className="ad-table">
                <thead>
                  <tr>
                    <th>Code</th>
                    <th>Note</th>
                    <th className="ad-num">Uses</th>
                    <th>Expires</th>
                    <th>Created</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {data.invites.map((i) => {
                    const spent = i.max_uses != null && i.uses >= i.max_uses;
                    const expired = !!i.expires_at && new Date(i.expires_at) < new Date();
                    return (
                      <tr key={i.code}>
                        <td className="ad-code" style={{ userSelect: 'all' }}>{i.code}</td>
                        <td className="ad-cell-muted">{i.note}</td>
                        <td className="ad-num">
                          {i.uses}/{i.max_uses ?? '∞'}
                          {spent && <span className="ad-tag" style={{ marginLeft: 6 }}>used</span>}
                        </td>
                        <td className="ad-cell-muted" title={dateTime(i.expires_at)}>
                          {i.expires_at ? relativeTime(i.expires_at).replace(' ago', '') : 'never'}
                          {expired && <span className="ad-tag ad-tag-danger" style={{ marginLeft: 6 }}>expired</span>}
                        </td>
                        <td className="ad-cell-muted">{relativeTime(i.created_at)}</td>
                        <td>
                          <button type="button" className="btn-ghost" style={{ height: 28, fontSize: 12 }} onClick={() => void api.adminRevokeInvite(i.code).then(load)}>
                            Revoke
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
