import { useState, useEffect } from 'react';
import { api } from '../api/client';

export default function QuizOverlay({ lectureId, chunk, onClose }) {
  const [q, setQ] = useState(null);
  const [selected, setSelected] = useState(null);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [startTs, setStartTs] = useState(null);

  useEffect(() => {
    // React 18 StrictMode fires effects twice in dev, which was causing a second
    // Ollama call to overwrite the first question. The `cancelled` flag makes the
    // first invocation a no-op if it resolves after the effect has been re-run.
    let cancelled = false;
    (async () => {
      setBusy(true); setErr('');
      try {
        const data = await api('/api/quiz/generate', {
          method: 'POST',
          body: { lecture_id: lectureId, chunk_id: chunk.id },
        });
        if (cancelled) return;
        setQ(data);
        setStartTs(Date.now());
      } catch (e) {
        if (!cancelled) setErr(e.message);
      } finally {
        if (!cancelled) setBusy(false);
      }
    })();
    return () => { cancelled = true; };
  }, [lectureId, chunk.id]);

  const submit = async () => {
    if (selected === null) return;
    setBusy(true);
    try {
      const r = await api('/api/quiz/submit', {
        method: 'POST',
        body: {
          question_id: q.id,
          lecture_id: lectureId,
          chunk_id: chunk.id,
          concept_id: chunk.concept_id,
          selected_index: selected,
          correct_index: -1,
          time_taken_sec: (Date.now() - startTs) / 1000,
        },
      });
      setResult(r);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  const optClass = (i) => {
    if (result === null) return selected === i ? 'quiz-option selected' : 'quiz-option';
    if (i === result.correct_index) return 'quiz-option correct';
    if (i === selected) return 'quiz-option wrong';
    return 'quiz-option';
  };

  const interventionText = (rule) => {
    const map = {
      OfferSimplerAnalogy: 'Frustrated? PAL will use a simpler analogy for the next question.',
      AddRemedialContent: 'PAL is inserting a prerequisite refresher next.',
      AdvanceDifficulty: 'You are on fire — difficulty going up!',
      SkipRedundant: 'Mastered — skipping repetitive content.',
      ContinueBaseline: 'On track. Keep going.',
    };
    return map[rule] || 'PAL is adapting the next step.';
  };

  return (
    <div className="quiz-overlay">
      <div className="card quiz-box">
        {busy && !q && <div>Generating a question with PAL…</div>}
        {err && <div className="error">{err}</div>}
        {q && !result && (
          <>
            <div className="diff">
              PAL · adaptive quiz · difficulty {q.difficulty}/5 · {q.concept}
            </div>
            <h3>{q.question}</h3>
            {q.options.map((opt, i) => (
              <div key={i} className={optClass(i)} onClick={() => setSelected(i)}>
                <b style={{ marginRight: 8 }}>{'ABCD'[i]}.</b>{opt}
              </div>
            ))}
            <div className="quiz-actions">
              <button className="ghost" onClick={() => onClose(null)}>Skip</button>
              <button onClick={submit} disabled={selected === null || busy}>Submit</button>
            </div>
          </>
        )}
        {result && (
          <>
            <div className={`quiz-result-banner ${result.correct ? 'ok' : 'bad'}`}>
              <div className="verdict">{result.correct ? '✓ Correct!' : '✗ Not quite'}</div>
              <div>{result.explanation}</div>
            </div>
            <div style={{ marginBottom: 8 }}>
              <b>Mastery:</b> {(result.mastery * 100).toFixed(0)}%
              &nbsp;·&nbsp;
              <b>Next difficulty:</b> {result.next_difficulty}/5
            </div>
            <div className="muted" style={{ fontSize: 13, marginBottom: 12 }}>
              {interventionText(result.intervention.rule)}
            </div>
            {q.options.map((opt, i) => (
              <div key={i} className={optClass(i)}>
                <b style={{ marginRight: 8 }}>{'ABCD'[i]}.</b>{opt}
              </div>
            ))}
            <div className="quiz-actions">
              <button onClick={() => onClose(result)}>Continue watching</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}