import { useState, useRef, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { useAuth } from '../hooks/useAuth';

export default function UserMenu() {
  const { user, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  if (!user) return null;

  const initial = (user.display_name || user.username || '?')[0].toUpperCase();

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 rounded-lg px-2 py-1.5 hover:bg-[var(--color-surface-2)] transition-colors"
      >
        <div className="w-6 h-6 rounded-full bg-[var(--color-accent)] flex items-center justify-center text-xs font-bold text-white">
          {initial}
        </div>
        <span className="hidden sm:inline text-sm text-[var(--color-muted)] max-w-[120px] truncate">
          {user.display_name || user.username}
        </span>
      </button>

      {open && (
        <div className="absolute right-0 mt-2 w-48 glass-strong rounded-xl shadow-lg overflow-hidden z-50">
          <div className="p-3 border-b border-[var(--color-border)]">
            <p className="text-sm font-medium truncate">{user.display_name || user.username}</p>
            <p className="text-xs text-[var(--color-muted-2)] truncate">@{user.username}</p>
          </div>
          <div className="p-1">
            <Link
              to="/my-jobs"
              onClick={() => setOpen(false)}
              className="block px-3 py-2 rounded-lg text-sm text-[var(--color-muted)] hover:text-white hover:bg-[var(--color-surface-2)] transition-colors"
            >
              My Jobs
            </Link>
            <button
              onClick={() => { setOpen(false); logout(); }}
              className="w-full text-left px-3 py-2 rounded-lg text-sm text-[var(--color-muted)] hover:text-[var(--color-danger)] hover:bg-[var(--color-surface-2)] transition-colors"
            >
              Sign out
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
