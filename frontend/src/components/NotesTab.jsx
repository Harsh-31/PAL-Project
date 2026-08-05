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
            <div className="meta">
              <span>at {fmt(n.timestamp_sec)}</span>
              <span style={{ cursor: 'pointer', color: 'var(--danger)' }} onClick={() => del(n.id)}>Delete</span>
            </div>
            <div style={{ whiteSpace: 'pre-wrap' }}>{n.content}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
