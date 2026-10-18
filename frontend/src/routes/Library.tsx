import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import './Library.css';
import * as api from '../api';
import { useClients } from '../clients/ClientContext';

const SWATCHES = [
  'linear-gradient(135deg,#ff9f6b,#ff5f6d)',
  'linear-gradient(135deg,#6bd9ff,#4a7dff)',
  'linear-gradient(135deg,#9be15d,#00e3ae)',
  'linear-gradient(135deg,#f9d976,#f39f86)',
  'linear-gradient(135deg,#a685e2,#7c5cff)',
  'linear-gradient(135deg,#ffd66b,#ff9a3c)',
  'linear-gradient(135deg,#6bffb8,#2fd4c7)',
  'linear-gradient(135deg,#ff8bd6,#ff5f9e)',
];
function swatchFor(id: number) {
  return SWATCHES[id % SWATCHES.length];
}

// get_recipe returns the file verbatim, frontmatter fence included — that's
// correct for the tool contract (an LLM must never paraphrase it), but the
// structured fields (cuisine, tags, ingredients) are already shown above in
// the panel, so the raw YAML is just noise for a human reader here.
function stripFrontmatter(markdown: string): string {
  return markdown.replace(/^---\n[\s\S]*?\n---\n/, '').trim();
}

type Section = 'all' | 'favorites' | { cuisine: string } | { book: string };

const ICONS = {
  list: 'M4 6h16M4 12h16M4 18h16',
  star: 'm12 3 2.9 6.3 6.9.8-5.1 4.8 1.4 6.9L12 18.3 5.9 21.8l1.4-6.9-5.1-4.8 6.9-.8Z',
};

export default function Library() {
  // The catalog is shared, but the view of it isn't: allergens are filtered
  // by the selected client's profile and the stars are that client's.
  const { activeClient } = useClients();
  const [query, setQuery] = useState('');
  const [debouncedQuery, setDebouncedQuery] = useState('');
  const [section, setSection] = useState<Section>('all');
  const [view, setView] = useState<'grid' | 'list'>('grid');
  const [facets, setFacets] = useState<api.RecipeFacets | null>(null);
  const [results, setResults] = useState<api.RecipeSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [selected, setSelected] = useState<api.RecipeDetail | null>(null);
  const [editingMacros, setEditingMacros] = useState(false);
  const [macroDraft, setMacroDraft] = useState({ kcal: '', protein_g: '', carb_g: '', fat_g: '', fiber_g: '' });
  const [macroBusy, setMacroBusy] = useState(false);
  const [macroError, setMacroError] = useState<string | null>(null);

  useEffect(() => {
    api.getFacets().then(setFacets).catch(() => setFacets({ cuisines: [], books: [] }));
  }, []);

  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(query), 250);
    return () => clearTimeout(t);
  }, [query]);

  useEffect(() => {
    if (!activeClient) {
      setResults([]);
      setTotal(0);
      setLoading(false);
      return;
    }
    setLoading(true);
    setSelectedId(null);
    const isFavorites = section === 'favorites';
    const params =
      typeof section === 'object' && 'cuisine' in section
        ? { cuisine: section.cuisine }
        : typeof section === 'object' && 'book' in section
          ? { book: section.book }
          : {};
    api
      // Favorites are filtered in SQL. Fetching a page and keeping the
      // starred rows only worked when the catalog was small — over 75k
      // recipes a star is almost never on the first page, which looked
      // exactly like favorites not saving.
      .browseRecipes({
        clientId: activeClient.id,
        query: debouncedQuery,
        ...params,
        favoritesOnly: isFavorites,
        limit: isFavorites ? 100 : 24,
      })
      .then((res) => {
        setResults(res.results);
        setTotal(res.total);
      })
      .catch(() => {
        setResults([]);
        setTotal(0);
      })
      .finally(() => setLoading(false));
  }, [debouncedQuery, section, activeClient]);

  useEffect(() => {
    setEditingMacros(false);
    setMacroError(null);
    if (selectedId == null || !activeClient) {
      setSelected(null);
      return;
    }
    api.getRecipe(selectedId, activeClient.id).then(setSelected).catch(() => setSelected(null));
  }, [selectedId, activeClient]);

  function startEditingMacros() {
    setMacroError(null);
    setMacroDraft({
      kcal: String(selected?.macros?.kcal ?? ''),
      protein_g: String(selected?.macros?.protein_g ?? ''),
      carb_g: String(selected?.macros?.carb_g ?? ''),
      fat_g: String(selected?.macros?.fat_g ?? ''),
      fiber_g: String(selected?.macros?.fiber_g ?? ''),
    });
    setEditingMacros(true);
  }

  async function saveMacroOverride() {
    if (!selected) return;
    const n = (v: string) => Number(v);
    const kcal = n(macroDraft.kcal);
    const protein_g = n(macroDraft.protein_g);
    const carb_g = n(macroDraft.carb_g);
    const fat_g = n(macroDraft.fat_g);
    const fiber_g = macroDraft.fiber_g.trim() === '' ? undefined : n(macroDraft.fiber_g);
    if ([kcal, protein_g, carb_g, fat_g].some((v) => Number.isNaN(v))) {
      setMacroError('Every field needs a number.');
      return;
    }
    setMacroBusy(true);
    setMacroError(null);
    try {
      const result = await api.setMacroOverride(selected.id, { kcal, protein_g, carb_g, fat_g, fiber_g });
      setSelected({ ...selected, macros: result.macros });
      setEditingMacros(false);
    } catch (err) {
      setMacroError(
        err instanceof api.ApiError && err.status === 400
          ? "That doesn't add up — kcal should roughly match 4×protein + 4×carb + 9×fat."
          : "Couldn't save the override.",
      );
    } finally {
      setMacroBusy(false);
    }
  }

  async function resetMacroOverride() {
    if (!selected) return;
    setMacroBusy(true);
    setMacroError(null);
    try {
      await api.clearMacroOverride(selected.id);
      const fresh = await api.getRecipe(selected.id, activeClient!.id);
      setSelected(fresh);
      setEditingMacros(false);
    } catch {
      setMacroError("Couldn't reset to the calculated estimate.");
    } finally {
      setMacroBusy(false);
    }
  }

  const sectionTitle = useMemo(() => {
    if (section === 'all') return 'All Recipes';
    if (section === 'favorites') return `${activeClient?.name ?? 'Client'}'s favorites`;
    if ('cuisine' in section) return section.cuisine;
    return section.book;
  }, [section, activeClient]);

  async function toggleFavorite(id: number, current: boolean, ev: React.MouseEvent) {
    ev.stopPropagation();
    if (!activeClient) return;
    const next = !current;
    setResults((rs) => rs.map((r) => (r.id === id ? { ...r, favorite: next } : r)));
    if (selected?.id === id) setSelected({ ...selected, favorite: next });
    try {
      await api.setFavorite(id, next, activeClient.id);
    } catch {
      setResults((rs) => rs.map((r) => (r.id === id ? { ...r, favorite: current } : r)));
    }
  }

  if (!activeClient) {
    return (
      <div className="lib-app" style={{ display: 'block', padding: 40 }}>
        <h2 style={{ fontSize: 18, margin: '0 0 6px' }}>Pick a client first</h2>
        <p style={{ fontSize: 14, color: 'var(--muted)', margin: '0 0 14px', maxWidth: 460 }}>
          The library hides recipes a client is allergic to and shows the ones you starred for
          them, so it needs to know whose shelf you're browsing.
        </p>
        <Link to="/app/clients" className="btn-primary" style={{ display: 'inline-flex', alignItems: 'center', height: 40, padding: '0 22px', textDecoration: 'none', fontSize: 14 }}>
          Open Clients
        </Link>
      </div>
    );
  }

  return (
    <div className="lib-app">
      <div className="lib-sidebar">
        <div className="lib-search-field">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <circle cx="11" cy="11" r="7" />
            <path d="m21 21-4.3-4.3" />
          </svg>
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search recipes" />
        </div>

        <div className="lib-side-group">
          <div className="lib-side-heading">Library</div>
          <button type="button" className={`lib-side-row ${section === 'all' ? 'lib-side-row-on' : ''}`} onClick={() => setSection('all')}>
            <span className="lib-side-icon">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d={ICONS.list} />
              </svg>
            </span>
            <span className="lib-side-label">All Recipes</span>
          </button>
          <button type="button" className={`lib-side-row ${section === 'favorites' ? 'lib-side-row-on' : ''}`} onClick={() => setSection('favorites')}>
            <span className="lib-side-icon">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d={ICONS.star} />
              </svg>
            </span>
            <span className="lib-side-label">Favorites</span>
          </button>
        </div>

        <div className="lib-side-group">
          <div className="lib-side-heading">Cuisine</div>
          {facets?.cuisines.slice(0, 14).map((c) => (
            <button
              key={c.value}
              type="button"
              className={`lib-side-row ${typeof section === 'object' && 'cuisine' in section && section.cuisine === c.value ? 'lib-side-row-on' : ''}`}
              onClick={() => setSection({ cuisine: c.value })}
            >
              <span className="lib-side-label">{c.value}</span>
              <span className="lib-side-count">{c.count}</span>
            </button>
          ))}
        </div>

        <div className="lib-side-group">
          <div className="lib-side-heading">Cookbooks</div>
          {facets?.books.slice(0, 14).map((b) => (
            <button
              key={b.value}
              type="button"
              className={`lib-side-row ${typeof section === 'object' && 'book' in section && section.book === b.value ? 'lib-side-row-on' : ''}`}
              onClick={() => setSection({ book: b.value })}
              title={b.value}
            >
              <span className="lib-side-label">{b.value}</span>
              <span className="lib-side-count">{b.count}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="lib-main">
        <div className="lib-toolbar">
          <div>
            <div className="lib-toolbar-title">{sectionTitle}</div>
            <div className="lib-toolbar-count">
              {loading ? 'Loading…' : `${total} recipe${total === 1 ? '' : 's'}`}
            </div>
          </div>
          <div style={{ flex: 1 }} />
          <div className="lib-seg-wrap">
            <button type="button" className={`lib-seg-icon-btn ${view === 'grid' ? 'lib-seg-icon-btn-on' : ''}`} onClick={() => setView('grid')} aria-label="Grid view">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <rect x="3" y="3" width="7" height="7" rx="1.5" />
                <rect x="14" y="3" width="7" height="7" rx="1.5" />
                <rect x="3" y="14" width="7" height="7" rx="1.5" />
                <rect x="14" y="14" width="7" height="7" rx="1.5" />
              </svg>
            </button>
            <button type="button" className={`lib-seg-icon-btn ${view === 'list' ? 'lib-seg-icon-btn-on' : ''}`} onClick={() => setView('list')} aria-label="List view">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M4 6h16M4 12h16M4 18h16" />
              </svg>
            </button>
          </div>
        </div>

        <div className="lib-content">
          <div className="lib-grid-scroll">
            {results.length === 0 && !loading && (
              <div className="lib-empty-state">
                <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
                  <circle cx="11" cy="11" r="7" />
                  <path d="m21 21-4.3-4.3" />
                </svg>
                <div style={{ fontSize: 15, fontWeight: 600 }}>No recipes match</div>
                <div style={{ fontSize: 13 }}>Try a different search or filter.</div>
              </div>
            )}

            {view === 'grid' ? (
              <div className="lib-grid">
                {results.map((r) => (
                  <div
                    key={r.id}
                    className={`lib-rcard ${selectedId === r.id ? 'lib-rcard-selected' : ''}`}
                    role="button"
                    tabIndex={0}
                    onClick={() => setSelectedId(r.id)}
                  >
                    <div className="lib-rcard-swatch" style={{ background: swatchFor(r.id) }}>
                      <button type="button" className={`lib-fav-btn ${r.favorite ? 'lib-fav-btn-on' : ''}`} onClick={(e) => toggleFavorite(r.id, r.favorite, e)} aria-label="Favorite">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill={r.favorite ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round">
                          <path d={ICONS.star} />
                        </svg>
                      </button>
                    </div>
                    <div className="lib-rcard-body">
                      <div className="lib-rcard-title">{r.title}</div>
                      <div className="lib-rcard-sub">
                        {r.book} · {r.cuisine ?? 'Uncategorized'}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                {results.map((r) => (
                  <div
                    key={r.id}
                    role="button"
                    tabIndex={0}
                    className={`lib-list-row ${selectedId === r.id ? 'lib-list-row-selected' : ''}`}
                    onClick={() => setSelectedId(r.id)}
                    onKeyDown={(e) => e.key === 'Enter' && setSelectedId(r.id)}
                  >
                    <div className="lib-list-swatch" style={{ background: swatchFor(r.id) }} />
                    <div style={{ flex: 1, minWidth: 0, textAlign: 'left' }}>
                      <div className="lib-list-title">{r.title}</div>
                      <div className="lib-list-sub">
                        {r.book} · {r.cuisine ?? 'Uncategorized'}
                      </div>
                    </div>
                    <button
                      type="button"
                      className={`lib-fav-btn ${r.favorite ? 'lib-fav-btn-on' : ''}`}
                      onClick={(e) => toggleFavorite(r.id, r.favorite, e)}
                      aria-label="Favorite"
                      style={{ background: 'transparent', color: r.favorite ? undefined : 'var(--muted)' }}
                    >
                      <svg width="15" height="15" viewBox="0 0 24 24" fill={r.favorite ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round">
                        <path d={ICONS.star} />
                      </svg>
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          {selected && (
            <div className="lib-detail">
              <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                <button type="button" className="lib-detail-close" onClick={() => setSelectedId(null)} aria-label="Close">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round">
                    <path d="M18 6 6 18M6 6l12 12" />
                  </svg>
                </button>
              </div>
              <div className="lib-detail-hero" style={{ background: swatchFor(selected.id) }} />
              <div className="lib-detail-title">{selected.title}</div>
              <div className="lib-detail-sub">
                {selected.book} · {selected.cuisine ?? 'Uncategorized'}
                {selected.servings ? ` · Serves ${selected.servings}` : ''}
              </div>

              <div className="lib-detail-section-title">
                Macros (per serving)
                {selected.macros?.confidence === 'override' && <span className="lib-macro-badge">manually corrected</span>}
              </div>
              {!editingMacros ? (
                <>
                  {selected.macros ? (
                    <div className="lib-stat-grid">
                      <div className="lib-stat-tile">
                        <div className="lib-stat-num">{Math.round(selected.macros.kcal)}</div>
                        <div className="lib-stat-label">kcal</div>
                      </div>
                      <div className="lib-stat-tile">
                        <div className="lib-stat-num">{Math.round(selected.macros.protein_g)}g</div>
                        <div className="lib-stat-label">protein</div>
                      </div>
                      <div className="lib-stat-tile">
                        <div className="lib-stat-num">{Math.round(selected.macros.carb_g)}g</div>
                        <div className="lib-stat-label">carb</div>
                      </div>
                      <div className="lib-stat-tile">
                        <div className="lib-stat-num">{Math.round(selected.macros.fat_g)}g</div>
                        <div className="lib-stat-label">fat</div>
                      </div>
                    </div>
                  ) : (
                    <p className="lib-macro-hint">No estimate yet.</p>
                  )}
                  {selected.macros && selected.macros.confidence !== 'override' && (
                    <p className="lib-macro-hint">
                      {selected.macros.confidence === 'low'
                        ? 'Low confidence — USDA lookup was incomplete for this recipe.'
                        : 'USDA-backed estimate.'}
                    </p>
                  )}
                  <div className="lib-macro-actions">
                    <button type="button" className="lib-macro-btn" onClick={startEditingMacros}>
                      {selected.macros?.confidence === 'override' ? 'Edit override' : 'Correct macros'}
                    </button>
                    {selected.macros?.confidence === 'override' && (
                      <button type="button" className="lib-macro-btn" disabled={macroBusy} onClick={() => void resetMacroOverride()}>
                        Reset to calculated
                      </button>
                    )}
                  </div>
                </>
              ) : (
                <div className="lib-macro-form">
                  <div className="lib-macro-form-grid">
                    {(['kcal', 'protein_g', 'carb_g', 'fat_g', 'fiber_g'] as const).map((key) => (
                      <div key={key} className="lib-macro-field">
                        <label>{key === 'kcal' ? 'kcal' : key.replace('_g', ' g')}</label>
                        <input
                          className="text-input"
                          inputMode="decimal"
                          value={macroDraft[key]}
                          onChange={(e) => setMacroDraft((d) => ({ ...d, [key]: e.target.value }))}
                        />
                      </div>
                    ))}
                  </div>
                  {macroError && <p className="lib-macro-error">{macroError}</p>}
                  <div className="lib-macro-actions">
                    <button type="button" className="lib-macro-btn lib-macro-btn-primary" disabled={macroBusy} onClick={() => void saveMacroOverride()}>
                      {macroBusy ? 'Saving…' : 'Save override'}
                    </button>
                    <button type="button" className="lib-macro-btn" disabled={macroBusy} onClick={() => setEditingMacros(false)}>
                      Cancel
                    </button>
                  </div>
                  <p className="lib-macro-hint">
                    Applies to this recipe everywhere it's used, not just for {activeClient?.name ?? 'this client'}.
                  </p>
                </div>
              )}

              <div className="lib-detail-section-title">Ingredients</div>
              <div className="lib-tag-row" style={{ marginBottom: 8 }}>
                {selected.tags.map((t) => (
                  <span className="lib-tag" key={t}>
                    {t}
                  </span>
                ))}
              </div>
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13.5, lineHeight: 1.7 }}>
                {selected.ingredients.map((ing, i) => (
                  <li key={i}>{ing}</li>
                ))}
              </ul>

              <div className="lib-detail-section-title">Full recipe</div>
              <div className="lib-markdown">{stripFrontmatter(selected.markdown)}</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
