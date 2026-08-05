import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';

export default function Layout() {
  const { user, logout } = useAuth();
  const loc = useLocation();
  const nav = useNavigate();
  return (
    <div className="app-shell">
      <div className="topbar">
        <div className="brand">PAL<span className="accent">MS</span></div>
        <nav>
          <NavLink to="/" end>{({ isActive }) => <button className={isActive ? 'active' : ''}>Dashboard</button>}</NavLink>
          <NavLink to="/learn">{({ isActive }) => <button className={isActive ? 'active' : ''}>Learn</button>}</NavLink>
          <NavLink to="/explorer">{({ isActive }) => <button className={isActive ? 'active' : ''}>KG Explorer</button>}</NavLink>
        </nav>
        <div className="user-menu">
          <span>{user?.name}</span>
          <button className="ghost" onClick={() => nav('/onboarding')}>Change course</button>
          <button className="ghost" onClick={logout}>Sign out</button>
        </div>
      </div>
      <div style={{ overflow: loc.pathname === '/learn' ? 'hidden' : 'auto' }}>
        <Outlet />
      </div>
    </div>
  );
}