import { NavLink, Outlet, useNavigate } from 'react-router-dom';
import { useEffect, useState } from 'react';
import './Admin.css';
import * as api from '../../api';
import { useAuth } from '../../auth/AuthContext';
import Logo from '../../components/Logo';

/** The console's own shell — separate nav, separate sense of place, same
 * visual grammar as AppShell (logo, wordmark, pill tab group, ghost button on
 * the right). The "separate site" feeling in the plan comes from this shell,
 * not from a separate deployment. */
export default function AdminShell() {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [openFeedback, setOpenFeedback] = useState(0);

  // A count on the Feedback tab, so an untriaged thumb-down is visible from
  // whichever screen the operator happens to be on. Fetched once per mount —
  // the Feedback screen itself is the live view.
  useEffect(() => {
    let cancelled = false;
    api
      .adminOverview()
      .then((o) => !cancelled && setOpenFeedback(o.feedback_open))
      .catch(() => {
        /* the badge is a nicety; a failed poll must not break the shell */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function onLogout() {
    await logout();
    navigate('/login', { replace: true });
  }

  const tab = ({ isActive }: { isActive: boolean }) => `ad-tab ${isActive ? 'ad-tab-on' : ''}`;

  return (
    <div className="ad-shell">
      <div className="ad-topbar">
        <Logo size={30} />
        <span className="ad-wordmark">Aboyeur</span>
        <span className="ad-badge">Admin</span>

        <nav className="ad-nav">
          {/* `end` on the index tab only — without it Dashboard stays lit on
              every child route, since they all start with /admin. */}
          <NavLink to="/admin" end className={tab}>
            Dashboard
          </NavLink>
          <NavLink to="/admin/users" className={tab}>
            Users
          </NavLink>
          <NavLink to="/admin/activity" className={tab}>
            Activity
          </NavLink>
          <NavLink to="/admin/feedback" className={tab}>
            Feedback
            {openFeedback > 0 && <span className="ad-tab-count">{openFeedback}</span>}
          </NavLink>
        </nav>

        <div className="ad-topbar-spacer" />

        <NavLink to="/app" className="ad-exit">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="m15 18-6-6 6-6" />
          </svg>
          Back to app
        </NavLink>
        <button
          type="button"
          onClick={onLogout}
          className="btn-ghost"
          style={{ height: 32, padding: '0 14px', fontSize: 13 }}
        >
          Sign out
        </button>
      </div>

      <div className="ad-body">
        <Outlet />
      </div>
    </div>
  );
}
