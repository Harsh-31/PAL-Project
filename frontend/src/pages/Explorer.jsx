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
            <div className="concept">{m.concept}</div>
            <div className="bar-track"><div className="bar-fill" style={{ width: `${m.score * 100}%` }} /></div>
            <div className="score">{(m.score * 100).toFixed(0)}% · {m.attempts} attempts</div>
          </div>
        ))}
      </div>

      <div className="card dash-section" style={{ marginTop: 16 }}>
        <h2>Process KG — deterministic rules</h2>
        <p className="muted">
          These edges live in Neo4j as <code>(CognitiveState)-[:TRIGGERS]-&gt;(InterventionRule)</code>.
          PAL consults them after every attempt to select the next action.
        </p>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ textAlign: 'left', color: 'var(--muted)' }}>
              <th style={{ padding: 6 }}>State</th>
              <th style={{ padding: 6 }}>Rule</th>
              <th style={{ padding: 6 }}>Fires when mastery ≤</th>
              <th style={{ padding: 6 }}>Action</th>
            </tr>
          </thead>
          <tbody>
            {[
              ['Frustrated', 'OfferSimplerAnalogy', '0.40', 'simplify_with_hobby_analogy'],
              ['Struggling', 'AddRemedialContent', '0.55', 'insert_prerequisite_video'],
              ['OnTrack',    'ContinueBaseline',   '0.70', 'continue_normal'],
              ['Confident',  'AdvanceDifficulty',  '0.85', 'raise_question_difficulty'],
              ['Mastered',   'SkipRedundant',      '0.95', 'skip_next_similar_chunk'],
            ].map(row => (
              <tr key={row[1]} style={{ borderTop: '1px solid var(--border)' }}>
                {row.map((c, i) => <td key={i} style={{ padding: 6, fontFamily: i > 0 ? 'monospace' : undefined }}>{c}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
