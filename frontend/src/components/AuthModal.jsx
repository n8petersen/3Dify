import { useState } from 'react';
import { useAuth } from '../hooks/useAuth';

export default function AuthModal({ onClose }) {
  const { login, register } = useAuth();
  const [mode, setMode] = useState('login'); // 'login' | 'register'
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setSubmitting(true);
    try {
      if (mode === 'login') {
        await login(username, password);
      } else {
        await register(username, password, displayName);
      }
      onClose();
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" onClick={onClose}>
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />

      {/* Modal */}
      <div
        className="relative glass-strong rounded-2xl w-full max-w-sm p-6 page-enter"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Close */}
        <button
          onClick={onClose}
          className="absolute top-3 right-3 text-[var(--color-muted-2)] hover:text-white transition-colors"
        >
          <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>

        {/* Tabs */}
        <div className="flex gap-1 mb-6 p-1 rounded-lg bg-[var(--color-surface-2)]">
          <button
            onClick={() => { setMode('login'); setError(''); }}
            className={`flex-1 py-2 rounded-md text-sm font-medium transition-colors ${
              mode === 'login'
                ? 'bg-[var(--color-surface-3)] text-white'
                : 'text-[var(--color-muted)] hover:text-white'
            }`}
          >
            Sign in
          </button>
          <button
            onClick={() => { setMode('register'); setError(''); }}
            className={`flex-1 py-2 rounded-md text-sm font-medium transition-colors ${
              mode === 'register'
                ? 'bg-[var(--color-surface-3)] text-white'
                : 'text-[var(--color-muted)] hover:text-white'
            }`}
          >
            Create account
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-xs text-[var(--color-muted-2)] mb-1.5">Username</label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              minLength={3}
              maxLength={24}
              autoComplete="username"
              className="w-full px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border-2)] text-sm focus:outline-none focus:border-[var(--color-accent)] transition-colors"
              placeholder="letters, numbers, - or _"
            />
          </div>

          <div>
            <label className="block text-xs text-[var(--color-muted-2)] mb-1.5">Password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={8}
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              className="w-full px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border-2)] text-sm focus:outline-none focus:border-[var(--color-accent)] transition-colors"
              placeholder="at least 8 characters"
            />
          </div>

          {mode === 'register' && (
            <div>
              <label className="block text-xs text-[var(--color-muted-2)] mb-1.5">
                Display name <span className="text-[var(--color-muted-2)]">(optional)</span>
              </label>
              <input
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                autoComplete="name"
                className="w-full px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border-2)] text-sm focus:outline-none focus:border-[var(--color-accent)] transition-colors"
                placeholder="how you want to be shown"
              />
            </div>
          )}

          {error && (
            <p className="text-sm text-[var(--color-danger)]">{error}</p>
          )}

          <button
            type="submit"
            disabled={submitting}
            className="w-full btn-accent py-2.5 rounded-lg text-sm font-medium disabled:opacity-50 transition-opacity"
          >
            {submitting
              ? (mode === 'login' ? 'Signing in...' : 'Creating account...')
              : (mode === 'login' ? 'Sign in' : 'Create account')
            }
          </button>
        </form>

        {mode === 'register' && (
          <p className="text-xs text-[var(--color-muted-2)] text-center mt-4">
            Forgot your password? Just make a new account.
          </p>
        )}

        {mode === 'login' && (
          <p className="text-xs text-[var(--color-muted-2)] text-center mt-4">
            Forgot your password? <button onClick={() => setMode('register')} className="text-[var(--color-accent)] hover:text-white transition-colors">Make a new account</button>
          </p>
        )}
      </div>
    </div>
  );
}
