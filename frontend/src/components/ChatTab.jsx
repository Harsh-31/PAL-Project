import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';

export default function ChatTab({ lectureId, chunkId }) {
  const [msgs, setMsgs] = useState([]);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const bodyRef = useRef(null);

  useEffect(() => {
    (async () => {
      try {
        const h = await api('/api/sidebar/chat/history?limit=30');
        setMsgs(h);
      } catch {}
    })();
  }, []);

  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [msgs]);

  const send = async () => {
    if (!draft.trim()) return;
    const q = draft.trim();
    setDraft('');
    setMsgs(m => [...m, { role: 'user', content: q, id: `local-${Date.now()}` }]);
    setBusy(true);
    try {
      const r = await api('/api/sidebar/chat', {
        method: 'POST',
        body: { message: q, lecture_id: lectureId, chunk_id: chunkId },
      });
      setMsgs(m => [...m, { role: 'assistant', content: r.reply, id: `local-a-${Date.now()}` }]);
    } catch (e) {
      setMsgs(m => [...m, { role: 'assistant', content: `Error: ${e.message}`, id: `err-${Date.now()}` }]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div ref={bodyRef} style={{ flex: 1, overflowY: 'auto', paddingBottom: 8 }}>
        {msgs.length === 0 && <p className="muted">Ask PAL anything about this lecture.</p>}
        {msgs.map(m => (
          <div key={m.id} className={`chat-msg ${m.role}`}>{m.content}</div>
        ))}
        {busy && <div className="chat-msg assistant"><i className="muted">PAL is thinking…</i></div>}
      </div>
      <div className="chat-input">
        <input
          placeholder="Ask a doubt…"
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && !busy && send()}
        />
        <button onClick={send} disabled={busy || !draft.trim()}>Send</button>
      </div>
    </div>
  );
}
