import { useState } from 'react';

const MAX_LENGTH = 500;

export default function TextPromptInput({ onSubmit }) {
  const [prompt, setPrompt] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const trimmed = prompt.trim();
  const canSubmit = trimmed.length > 0 && trimmed.length <= MAX_LENGTH && !loading;

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!canSubmit) return;
    setError(null);
    setLoading(true);
    try {
      await onSubmit(trimmed);
    } catch (err) {
      setError(err.message || 'Something went wrong');
      setLoading(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="w-full max-w-xl mx-auto">
      <div className="glass-strong rounded-2xl p-6 glow-accent-sm space-y-4">
        <div className="relative">
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="A rubber duck wearing a top hat..."
            rows={4}
            maxLength={MAX_LENGTH}
            disabled={loading}
            className="w-full bg-[var(--color-surface-2)] border border-[var(--color-border)] rounded-xl px-4 py-3 text-sm text-white placeholder:text-[var(--color-muted-2)] focus:outline-none focus:border-[var(--color-accent)] focus:ring-1 focus:ring-[var(--color-accent)]/30 resize-none transition-colors disabled:opacity-50"
          />
          <span className={`absolute bottom-3 right-3 text-xs font-mono ${
            trimmed.length > MAX_LENGTH * 0.9
              ? 'text-[var(--color-warning)]'
              : 'text-[var(--color-muted-2)]'
          }`}>
            {trimmed.length}/{MAX_LENGTH}
          </span>
        </div>

        <p className="text-xs text-[var(--color-muted-2)] text-center">
          Describe a single object with a clear shape. The AI will generate an image first, then turn it into a 3D model.
        </p>

        {error && (
          <p className="text-sm text-[var(--color-danger)] text-center">{error}</p>
        )}

        <button
          type="submit"
          disabled={!canSubmit}
          className="w-full py-3.5 btn-accent text-sm font-semibold rounded-xl glow-accent-sm flex items-center justify-center gap-2 disabled:opacity-40 disabled:cursor-not-allowed transition-opacity"
        >
          {loading ? (
            <>
              <svg className="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              Submitting...
            </>
          ) : (
            <>
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 00-2.455 2.456z" />
              </svg>
              Generate 3D Model
            </>
          )}
        </button>
      </div>
    </form>
  );
}
