import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';

export default function Onboarding() {
  const { refresh } = useAuth();
  const nav = useNavigate();
  const [step, setStep] = useState(1);
  const [courses, setCourses] = useState([]);
  const [course, setCourse] = useState(null);
  const [baseline, setBaseline] = useState('beginner');
  const [goal, setGoal] = useState('');
  const [freq, setFreq] = useState('per_chunk');
  const [hobbies, setHobbies] = useState([]);
  const [hobbyDraft, setHobbyDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  useEffect(() => {
    api('/api/courses').then(setCourses).catch(e => setErr(e.message));
  }, []);

  const addHobby = () => {
    const v = hobbyDraft.trim();
    if (!v) return;
    if (hobbies.includes(v)) return;
    setHobbies([...hobbies, v]);
    setHobbyDraft('');
  };

  const finish = async () => {
    setBusy(true); setErr('');
    try {
      await api('/api/onboarding', {
        method: 'POST',
        body: {
          course_id: course.id,
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

  return (
    <div style={{ maxWidth: 900, margin: '0 auto', padding: 24 }}>
      <div style={{ marginBottom: 16, color: 'var(--muted)' }}>Step {step} of 4</div>

      {step === 1 && (
        <div>
          <h2>Pick a course to start with</h2>
          <p className="muted">You can add more later. Pick the one that matches your current focus.</p>
          <div className="course-grid" style={{ padding: 0, marginTop: 16 }}>
            {courses.map(c => (
              <div
                key={c.id}
                className={`card course-card`}
                style={course?.id === c.id ? { borderColor: 'var(--primary)' } : {}}
                onClick={() => setCourse(c)}
              >
                <div className="provider">{c.provider} · {c.code}</div>
                <h3>{c.title}</h3>
                <p className="muted">{c.description}</p>
              </div>
            ))}
          </div>
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 20 }}>
            <button disabled={!course} onClick={() => setStep(2)}>Continue</button>
          </div>
        </div>
      )}

      {step === 2 && (
        <div className="card">
          <h2>Your baseline</h2>
          <p className="muted">How would you describe your current knowledge of <b>{course.title}</b>?</p>
          <div className="field" style={{ marginTop: 16 }}>
            <label className="label">Level</label>
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
          <h2>Ready to go</h2>
          <div style={{ marginTop: 12 }}>
            <p><b>Course:</b> {course.title}</p>
            <p><b>Baseline:</b> {baseline}</p>
            <p><b>Goal:</b> {goal}</p>
            <p><b>Evaluation:</b> {freq.replace('_', ' ')}</p>
            <p><b>Hobbies:</b> {hobbies.length ? hobbies.join(', ') : '(none — PAL will keep explanations direct)'}</p>
          </div>
          {err && <div className="error">{err}</div>}
          <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 20 }}>
            <button className="ghost" onClick={() => setStep(3)}>Back</button>
            <button onClick={finish} disabled={busy}>{busy ? 'Setting up…' : 'Start learning'}</button>
          </div>
        </div>
      )}
    </div>
  );
}
