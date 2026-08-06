import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import BrandLogo from '../components/BrandLogo';

export default function Signup() {
  const { signup } = useAuth();
  const nav = useNavigate();
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [pw, setPw] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setErr(''); setBusy(true);
    try {
      await signup(name, email, pw);
      nav('/onboarding', { replace: true });
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="center-full">
      <div className="card auth-card">
        <div className="auth-brand">
          <div className="brand">
            <BrandLogo size={30} />
            <span className="brand-pipe">|</span>
            <span className="brand-pal">PAL</span>
          </div>
          <p>Personal Adaptive Learning · by IAIRO</p>
        </div>
        <h1 style={{ textAlign: 'center', fontSize: 20, marginBottom: 24 }}>Create your account</h1>
        <form onSubmit={submit}>
          <div className="field">
            <label className="label">Full name</label>
            <input value={name} onChange={e => setName(e.target.value)} required />
          </div>
          <div className="field">
            <label className="label">Email</label>
            <input type="email" value={email} onChange={e => setEmail(e.target.value)} required />
          </div>
          <div className="field">
            <label className="label">Password</label>
            <input type="password" value={pw} onChange={e => setPw(e.target.value)} required minLength={6} />
          </div>
          {err && <div className="error">{err}</div>}
          <button style={{ width: '100%', marginTop: 8 }} type="submit" disabled={busy}>
            {busy ? 'Creating…' : 'Create account'}
          </button>
        </form>
        <p className="muted" style={{ textAlign: 'center', marginTop: 16 }}>
          Have an account? <Link to="/login">Sign in</Link>
        </p>
      </div>
    </div>
  );
}