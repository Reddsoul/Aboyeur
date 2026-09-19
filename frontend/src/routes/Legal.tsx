import { useEffect, useState } from 'react';
import { NavLink, useParams } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import './Auth.css';
import * as api from '../api';

const TITLES: Record<string, string> = {
  terms: 'Terms',
  privacy: 'Privacy',
  disclaimer: 'Health disclaimer',
  refunds: 'Refunds',
  'acceptable-use': 'Acceptable use',
};

/** /legal/:slug — the markdown under legal/, rendered. Public: readable
 * logged out, which the signup form and the ToS modal both need. */
export default function Legal() {
  const { slug = 'terms' } = useParams();
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setText(null);
    setError(null);
    api
      .legalDoc(slug)
      .then(setText)
      .catch(() => setError("This document isn't available."));
    window.scrollTo(0, 0);
  }, [slug]);

  return (
    <div className="au-stage" style={{ alignItems: 'flex-start' }}>
      <div className="au-orb" />
      <div className="au-legal">
        <nav className="au-legal-nav">
          <NavLink to="/">Aboyeur</NavLink>
          <span style={{ color: 'var(--line)' }}>|</span>
          {Object.entries(TITLES).map(([s, t]) => (
            <NavLink key={s} to={`/legal/${s}`} className={({ isActive }) => (isActive ? 'on' : '')}>
              {t}
            </NavLink>
          ))}
        </nav>
        {error && <p className="au-error">{error}</p>}
        {text === null && !error && <p className="au-sub">Loading…</p>}
        {text && (
          <>
            {/DRAFT/.test(text) && <span className="au-draft">Draft — pending legal review</span>}
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
          </>
        )}
      </div>
    </div>
  );
}
