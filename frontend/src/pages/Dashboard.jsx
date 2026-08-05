import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';

export default function Dashboard() {
  const { user } = useAuth();
  const [data, setData] = useState(null);
  const [err, setErr] = useState('');

  useEffect(() => {
    api('/api/dashboard/summary').then(setData).catch(e => setErr(e.message));
  }, []);

  if (err) return <div style={{ padding: 20 }} className="error">{err}</div>;
  if (!data) return <div className="center-full">Loading…</div>;

  return (
    <div>
      <div style={{ padding: '20px 20px 0' }}>
        <h1 style={{ margin: 0 }}>Hi {user?.name?.split(' ')[0] || 'there'}!</h1>
        <p className="muted">
          {user?.track_ids?.length
            ? `Learning: ${user.track_ids.map(t => t.replace(/-/g, ' ')).join(', ')}`
            : 'Not enrolled yet'}
          {' · '}
          <Link to="/learn">Jump back into learning →</Link>
        </p>
      </div>

      <div className="dash-grid">
        <div className="card stat">
          <div className="label">Quiz attempts</div>
          <div className="val">{data.total_attempts}</div>
        </div>
        <div className="card stat">
          <div className="label">Accuracy</div>
          <div className="val">{(data.accuracy * 100).toFixed(0)}%</div>
        </div>
        <div className="card stat">
          <div className="label">Notes taken</div>
          <div className="val">{data.note_count}</div>
        </div>
        <div className="card stat">
          <div className="label">Concepts tracked</div>
          <div className="val">{data.mastery.length}</div>
        </div>
      </div>

      <div style={{ padding: '0 20px 20px' }}>
        <div className="card dash-section">
          <h2>Mastery by concept</h2>
          {data.mastery.length === 0 && <p className="muted">Take a quiz to start building mastery.</p>}
          {data.mastery.map(m => (
            <div key={m.concept} className="kg-bar">
              <div className="concept">{m.concept}</div>
              <div className="bar-track"><div className="bar-fill" style={{ width: `${m.score * 100}%` }} /></div>
              <div className="score">{(m.score * 100).toFixed(0)}%</div>
            </div>
          ))}
        </div>

        <div className="card dash-section" style={{ marginTop: 16 }}>
          <h2>Recent activity</h2>
          {data.recent_attempts.length === 0 && <p className="muted">Nothing yet.</p>}
          {data.recent_attempts.map((a, i) => (
            <div key={i} style={{
              display: 'flex', justifyContent: 'space-between',
              padding: '8px 0', borderBottom: '1px solid var(--border)'
            }}>
              <div>
                <span style={{ color: a.correct ? 'var(--success)' : 'var(--danger)' }}>
                  {a.correct ? '✓' : '✗'}
                </span>
                &nbsp; Concept: <b>{a.concept_id}</b> · diff {a.difficulty}
              </div>
              <div className="muted">
                {a.intervention || '—'} · {new Date(a.timestamp).toLocaleString()}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}