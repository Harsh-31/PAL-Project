import { useEffect, useState } from 'react';
import { api } from '../api/client';

export default function Explorer() {
  const [snap, setSnap] = useState(null);

  useEffect(() => {
    api('/api/dashboard/kg').then(setSnap);
  }, []);

  if (!snap) return <div className="center-full">Loading KG…</div>;

  return (
    <div style={{ padding: 20 }}>
      <h1 style={{ marginTop: 0 }}>Knowledge Graph Explorer</h1>
      <p className="muted">
        Neuro-symbolic User KG — your Learner node in Neo4j, its concept edges (MASTERS)
        and the deterministic Process KG that steers PAL.
      </p>

      <div className="card dash-section" style={{ marginTop: 16 }}>
        <h2>Enrolled courses</h2>
        {snap.enrolled.length === 0 && <p className="muted">None yet.</p>}
        <ul>
          {snap.enrolled.map((c, i) => <li key={i}>{c}</li>)}
        </ul>
      </div>

      <div className="card dash-section" style={{ marginTop: 16 }}>
        <h2>Concept mastery (A-Box)</h2>
        {snap.mastery.length === 0 && <p className="muted">No mastery edges yet — quiz to populate.</p>}
        {snap.mastery.map(m => (
          <div key={m.concept} className="kg-bar">
            <div className="concept">
              {m.concept}
              {m.cognitive_state && (
                <span style={{ marginLeft: 8, fontSize: '0.8em', color: 'var(--muted)' }}>
                  [{m.cognitive_state}]
                </span>
              )}
            </div>
            <div className="bar-track"><div className="bar-fill" style={{ width: `${m.score * 100}%` }} /></div>
            <div className="score">
              {(m.score * 100).toFixed(0)}% · {m.attempts} attempts
              {m.tau_struggling != null && (
                <span style={{ marginLeft: 6, fontSize: '0.8em', color: 'var(--muted)' }}>
                  (τ_s={m.tau_struggling?.toFixed(2)} τ_m={m.tau_mastered?.toFixed(2)})
                </span>
              )}
            </div>
          </div>
        ))}
      </div>

      <div className="card dash-section" style={{ marginTop: 16 }}>
        <h2>Process KG — adaptive rules</h2>
        <p className="muted">
          3-state Process KG with per-learner thresholds learned by the Threshold RL.
          Boundaries (τ_struggling, τ_mastered) are personalized per concept.
        </p>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ textAlign: 'left', color: 'var(--muted)' }}>
              <th style={{ padding: 6 }}>State</th>
              <th style={{ padding: 6 }}>Rule</th>
              <th style={{ padding: 6 }}>Condition</th>
              <th style={{ padding: 6 }}>Action</th>
            </tr>
          </thead>
          <tbody>
            {[
              ['Struggling', 'OfferSimplerAnalogy + AddRemedialContent', 'mastery < τ_struggling', 'simplify_with_hobby_analogy, insert_prerequisite_video'],
              ['Confident',  'ConfidentContinue',   'τ_struggling ≤ mastery < τ_mastered', 'continue_normal'],
              ['Mastered',   'MasteredChallenge',    'mastery ≥ τ_mastered', 'offer_challenge_content'],
            ].map(row => (
              <tr key={row[0]} style={{ borderTop: '1px solid var(--border)' }}>
                {row.map((c, i) => <td key={i} style={{ padding: 6, fontFamily: i > 0 ? 'monospace' : undefined }}>{c}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
