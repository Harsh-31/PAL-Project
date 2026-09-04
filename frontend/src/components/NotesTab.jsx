import { useEffect, useState } from 'react';
import { api } from '../api/client';

export default function NotesTab({ videoId, currentTime }) {
  const [notes, setNotes] = useState([]);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);

  const reload = async () => {
    if (!videoId) return;
    try {
      setNotes(await api(`/api/sidebar/notes/${videoId}`));
    } catch {}
  };

  useEffect(() => { reload(); }, [videoId]);

  const add = async () => {
    if (!draft.trim() || !videoId) return;
    setBusy(true);
    try {
      await api('/api/sidebar/notes', {
        method: 'POST',
        body: { video_id: videoId, content: draft.trim(), timestamp_sec: currentTime || 0 },
      });
      setDraft('');
      await reload();
    } finally {
      setBusy(false);
    }
  };

  const del = async (id) => {
    await api(`/api/sidebar/notes/${id}`, { method: 'DELETE' });
    await reload();
  };

  const fmt = (s) => {
    if (!s && s !== 0) return '';
    const m = Math.floor(s / 60), sec = Math.floor(s % 60);
    return `${m}:${sec.toString().padStart(2, '0')}`;
  };

  return (
    <div>
      <div className="note-form">
        <textarea
          placeholder="Take a note at the current timestamp…"
          value={draft}
          onChange={e => setDraft(e.target.value)}
        />
        <button onClick={add} disabled={busy || !draft.trim() || !videoId}>
          Add note{currentTime ? ` @ ${fmt(currentTime)}` : ''}
        </button>
      </div>
      <div style={{ marginTop: 16 }}>
        {notes.length === 0 && <p className="muted">No notes yet.</p>}
        {notes.map(n => (
          <div key={n.id} className="note">
            <span className="note-delete" onClick={() => del(n.id)} title="Delete note">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="3 6 5 6 21 6" />
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                <line x1="10" y1="11" x2="10" y2="17" />
                <line x1="14" y1="11" x2="14" y2="17" />
              </svg>
            </span>
            <div className="note-time">at {fmt(n.timestamp_sec)}</div>
            <div style={{ whiteSpace: 'pre-wrap' }}>{n.content}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
