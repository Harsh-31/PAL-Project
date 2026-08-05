import { useState } from 'react';
import { api } from '../api/client';

const SAMPLE = `# Try it out
def fib(n):
    return n if n < 2 else fib(n-1) + fib(n-2)

for i in range(8):
    print(i, fib(i))
`;

export default function CodeTab() {
  const [code, setCode] = useState(SAMPLE);
  const [out, setOut] = useState(null);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    setBusy(true);
    try {
      const r = await api('/api/sidebar/code/run', {
        method: 'POST',
        body: { language: 'python', code },
      });
      setOut(r);
    } catch (e) {
      setOut({ stdout: '', stderr: e.message, returncode: -1 });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <textarea
        className="code-editor"
        value={code}
        onChange={e => setCode(e.target.value)}
        spellCheck={false}
      />
      <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
        <button onClick={run} disabled={busy}>{busy ? 'Running…' : 'Run'}</button>
        <button className="ghost" onClick={() => setCode('')}>Clear</button>
      </div>
      {out && (
        <>
          {out.stdout && <div className="code-output">{out.stdout}</div>}
          {out.stderr && <div className="code-output err">{out.stderr}</div>}
          <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
            exit code: {out.returncode}
          </div>
        </>
      )}
    </div>
  );
}
