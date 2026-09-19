import { Link } from 'react-router-dom';
import './Auth.css';
import Logo from '../components/Logo';
import { LegalFooter } from '../components/Legal';

/** The logged-out front door at `/`. One screen: what it is, what it looks
 * like, who it's for (and not for), and the two doors in. */
export default function Landing() {
  return (
    <div className="au-stage" style={{ alignItems: 'flex-start', paddingTop: 48 }}>
      <div className="au-orb" />
      <div className="au-landing">
        <div className="au-hero">
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <Logo size={36} />
            <span style={{ fontSize: 16, fontWeight: 700 }}>Aboyeur</span>
          </div>
          <h1>Menus and macros for every client, from a real cookbook library.</h1>
          <p>
            Tell Aboyeur who you're planning for — goals, allergies, how many meals a day — and it
            builds a week of dinners from tens of thousands of real recipes, portions them to
            each person's targets, and writes the shopping list. Bring your own AI key or use ours.
          </p>
          <div className="au-cta">
            <Link to="/signup" className="btn-primary" style={{ textDecoration: 'none' }}>
              Create an account
            </Link>
            <Link to="/login" className="btn-ghost" style={{ textDecoration: 'none' }}>
              Sign in
            </Link>
          </div>
          <div className="au-features">
            <div className="au-feature">
              <strong>Clients, isolated</strong>
              Each person's profile, plans and conversations live apart. Nothing bleeds between them.
            </div>
            <div className="au-feature">
              <strong>Numbers you can check</strong>
              Calories and macros come from USDA data and are labelled with their confidence.
            </div>
            <div className="au-feature">
              <strong>Your model, or ours</strong>
              Use your own OpenAI-compatible key for free, or Pro with prepaid credit on hosted models.
            </div>
          </div>
          <p className="au-note" style={{ marginTop: 22, maxWidth: 460 }}>
            <strong>Not for clinical use.</strong> Aboyeur is for coaches, trainers, nutrition
            consultants and self-tracking — not healthcare providers. It isn't HIPAA-compliant and
            its output is an estimate, not medical advice.
          </p>
          <LegalFooter style={{ marginTop: 18 }} />
        </div>

        <div className="au-shot" aria-hidden="true">
          <div className="au-shot-bubble au-shot-user">Plan 5 dinners for Sam this week, high protein, no shellfish.</div>
          <div className="au-shot-bubble au-shot-bot">
            Here's Sam's week at ~2,100 kcal/day, 150 g protein:
            <table>
              <thead>
                <tr><th>Day</th><th>Dinner</th><th>kcal</th><th>P</th></tr>
              </thead>
              <tbody>
                <tr><td>1</td><td>Chicken cacciatore</td><td>690</td><td>52 g</td></tr>
                <tr><td>2</td><td>Lentil &amp; lamb stew</td><td>710</td><td>48 g</td></tr>
                <tr><td>3</td><td>Baked cod, potatoes</td><td>640</td><td>55 g</td></tr>
                <tr><td>4</td><td>Turkey chili</td><td>700</td><td>58 g</td></tr>
                <tr><td>5</td><td>Beef &amp; barley soup</td><td>680</td><td>50 g</td></tr>
              </tbody>
            </table>
            <div style={{ marginTop: 6, color: 'var(--muted)' }}>Shopping list: 23 items, 9 already in the pantry.</div>
          </div>
        </div>
      </div>
    </div>
  );
}
