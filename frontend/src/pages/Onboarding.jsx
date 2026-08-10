import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';

export default function Onboarding() {
  const { refresh } = useAuth();
  const nav = useNavigate();
  const [step, setStep] = useState(1);
  const [tracks, setTracks] = useState([]);
  const [selectedTracks, setSelectedTracks] = useState([]);
  const [baseline, setBaseline] = useState('beginner');
  const [goal, setGoal] = useState('');
  const [freq, setFreq] = useState('per_chunk');
  const [hobbies, setHobbies] = useState([]);
  const [hobbyDraft, setHobbyDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  useEffect(() => {
    api('/api/tracks').then(setTracks).catch(e => setErr(e.message));
  }, []);

  const toggleTrack = (id) => {
    setSelectedTracks(sel =>
      sel.includes(id) ? sel.filter(x => x !== id) : [...sel, id]
    );
  };

  const addHobby = () => {
    const v = hobbyDraft.trim();
    if (!v || hobbies.includes(v)) return;
    setHobbies([...hobbies, v]);
    setHobbyDraft('');
  };

  const finish = async () => {
    setBusy(true); setErr('');
    try {
      await api('/api/onboarding', {
        method: 'POST',
        body: {
          track_ids: selectedTracks,
          baseline,
          goal,
          evaluation_frequency: freq,
          hobbies,
        },
      });
      await refresh();
      nav('/learn', { replace: true });
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  const selectedTrackObjects = tracks.filter(t => selectedTracks.includes(t.id));

  return (
    <div style={{ maxWidth: 900, margin: '0 auto', padding: 24 }}>
      <div style={{ marginBottom: 16, color: 'var(--muted)' }}>Step {step} of 4</div>

      {step === 1 && (
        <div>
          <h2>What do you want to learn?</h2>
          <p className="muted">
            Pick tracks to guide your playlist, or skip and let PAL find the best
            content based on your learning goal. PAL will compose a custom playlist
            from MIT OCW, NPTEL, and other open courses.
          </p>
          <div className="course-grid" style={{ padding: 0, marginTop: 16 }}>
            {tracks.map(t => (
              <div
                key={t.id}
                className={`card course-card`}
                style={selectedTracks.includes(t.id) ? { borderColor: 'var(--primary)' } : {}}
                onClick={() => toggleTrack(t.id)}
              >
                <div className="provider">
                  {t.icon || '📚'} · {t.stats?.courses ?? 0} course{t.stats?.courses === 1 ? '' : 's'}
                  &nbsp;·&nbsp;{t.stats?.lectures ?? 0} lecture{t.stats?.lectures === 1 ? '' : 's'}
                </div>
                <h3>{t.title}</h3>
                <p className="muted">{t.description}</p>
                {selectedTracks.includes(t.id) && (
                  <div style={{ marginTop: 8, color: 'var(--primary)', fontSize: 13 }}>✓ Selected</div>
                )}
              </div>
            ))}
          </div>
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 20 }}>
            <button onClick={() => setStep(2)}>
              {selectedTracks.length > 0
                ? `Continue with ${selectedTracks.length} track${selectedTracks.length === 1 ? '' : 's'}`
                : 'Skip — let PAL match your goal'}
            </button>
          </div>
        </div>
      )}

      {step === 2 && (
        <div className="card">
          <h2>Your baseline & goal</h2>
          <p className="muted">
            Tell PAL where you are today and what you want to achieve. This shapes
            question difficulty and the summaries you'll see.
          </p>
          <div className="field" style={{ marginTop: 16 }}>
            <label className="label">Your current level in these topics</label>
            <select value={baseline} onChange={e => setBaseline(e.target.value)}>
              <option value="beginner">Beginner — new to the topic</option>
              <option value="intermediate">Intermediate — some prior exposure</option>
              <option value="advanced">Advanced — looking to sharpen</option>
            </select>
          </div>
          <div className="field">
            <label className="label">What's your goal?</label>
            <textarea rows={3} placeholder="e.g. Pass my end-sem exam, build a personal project, prep for interviews"
              value={goal} onChange={e => setGoal(e.target.value)} />
          </div>
          <div className="field">
            <label className="label">How often should PAL evaluate you?</label>
            <select value={freq} onChange={e => setFreq(e.target.value)}>
              <option value="per_chunk">After every lecture chunk (recommended)</option>
              <option value="per_video">After every full video</option>
              <option value="per_session">Once per session</option>
            </select>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 20 }}>
            <button className="ghost" onClick={() => setStep(1)}>Back</button>
            <button disabled={!goal.trim()} onClick={() => setStep(3)}>Continue</button>
          </div>
        </div>
      )}

      {step === 3 && (
        <div className="card">
          <h2>Your hobbies</h2>
          <p className="muted">PAL uses these to make analogies that click. Add 2-5 things you love.</p>
          <div style={{ marginTop: 16 }}>
            {hobbies.map(h => (
              <span key={h} className="hobby-tag">
                {h}
                <span className="x" onClick={() => setHobbies(hobbies.filter(x => x !== h))}>×</span>
              </span>
            ))}
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
            <input
              placeholder="e.g. Marvel, cricket, cooking, guitar"
              value={hobbyDraft}
              onChange={e => setHobbyDraft(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && (e.preventDefault(), addHobby())}
            />
            <button onClick={addHobby}>Add</button>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 20 }}>
            <button className="ghost" onClick={() => setStep(2)}>Back</button>
            <button onClick={() => setStep(4)}>Continue</button>
          </div>
        </div>
      )}

      {step === 4 && (
        <div className="card">
          <h2>Your learning plan</h2>
          <p className="muted">PAL will build your playlist from these tracks. You can change any of this later via <b>Change course</b>.</p>
          <div style={{ marginTop: 12 }}>
            {selectedTrackObjects.length > 0 ? (
              <>
                <p><b>Tracks:</b></p>
                <ul style={{ marginTop: 4, marginLeft: 20 }}>
                  {selectedTrackObjects.map(t => (
                    <li key={t.id}>{t.icon || '📚'} {t.title}</li>
                  ))}
                </ul>
              </>
            ) : (
              <p><b>Tracks:</b> None — PAL will match your goal across all courses</p>
            )}
            <p><b>Baseline:</b> {baseline}</p>
            <p><b>Goal:</b> {goal}</p>
            <p><b>Evaluation:</b> {freq.replace('_', ' ')}</p>
            <p><b>Hobbies:</b> {hobbies.length ? hobbies.join(', ') : '(none — PAL will keep explanations direct)'}</p>
          </div>
          {err && <div className="error">{err}</div>}
          <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 20 }}>
            <button className="ghost" onClick={() => setStep(3)}>Back</button>
            <button onClick={finish} disabled={busy}>{busy ? 'Setting up…' : 'Build my playlist'}</button>
          </div>
        </div>
      )}
    </div>
  );
}