import { useEffect, useState, useRef } from 'react';
import { Link } from 'react-router-dom';

const STATUS_META = {
  pending:    { label: 'Pending',    color: 'var(--color-warning)', dot: true },
  assigned:   { label: 'Starting',   color: 'var(--color-warning)', pulse: true },
  processing: { label: 'Generating', color: 'var(--color-accent)',  pulse: true },
  complete:   { label: 'Complete',   color: 'var(--color-success)' },
  failed:     { label: 'Failed',     color: 'var(--color-danger)' },
  expired:    { label: 'Expired',    color: 'var(--color-muted)' },
};

function timeAgo(iso) {
  if (!iso) return '';
  const ms = Date.now() - new Date(iso).getTime();
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

export default function ActivityCard({ job }) {
  const meta = STATUS_META[job.status] || STATUS_META.pending;
  const [hover, setHover] = useState(false);
  const [, tick] = useState(0);
  const cardRef = useRef(null);

  // Re-render every 10s for relative timestamps
  useEffect(() => {
    const id = setInterval(() => tick((n) => n + 1), 10_000);
    return () => clearInterval(id);
  }, []);

  // Image source: prefer thumbnail; fallback to generated image (for text jobs)
  const imageSrc = job.thumbnail_url || job.generated_image_url;
  const isText = job.job_type === 'text';
  const inFlight = job.status === 'processing' || job.status === 'assigned' || job.status === 'pending';
  const showModelViewer = hover && job.glb_url && job.status === 'complete';

  return (
    <div
      ref={cardRef}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      className={`activity-card group relative glass-strong rounded-2xl overflow-hidden border border-[var(--color-border)] transition-all duration-300 hover:border-[var(--color-accent)]/40 ${job._isNew ? 'activity-card-new' : ''} ${job._justCompleted ? 'activity-card-flash' : ''}`}
    >
      {/* Visual area */}
      <Link to={`/job/${job.id}`} target="_blank" rel="noopener noreferrer" className="block relative aspect-square bg-[var(--color-surface-2)] overflow-hidden">
        {showModelViewer ? (
          // eslint-disable-next-line react/no-unknown-property
          <model-viewer
            src={job.glb_url}
            auto-rotate
            auto-rotate-delay="0"
            rotation-per-second="60deg"
            disable-zoom
            disable-pan
            interaction-prompt="none"
            orientation="0deg -90deg 0deg"
            shadow-intensity="0.4"
            environment-image="neutral"
            style={{ width: '100%', height: '100%', background: '#0a0a0a', '--poster-color': 'transparent' }}
          />
        ) : imageSrc ? (
          <img
            src={imageSrc}
            alt=""
            loading="lazy"
            className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-105"
          />
        ) : (
          // Placeholder for text jobs without a generated image yet
          <div className="w-full h-full flex items-center justify-center bg-gradient-to-br from-[var(--color-surface-2)] to-black">
            <svg className="w-10 h-10 text-[var(--color-muted-2)]" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth="1.2">
              <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h16M4 18h10" />
            </svg>
          </div>
        )}

        {/* Top-left: status pill */}
        <div className="absolute top-2 left-2 flex items-center gap-1.5 px-2 py-1 rounded-md backdrop-blur-md bg-black/40 border border-white/5">
          <span
            className={`w-1.5 h-1.5 rounded-full ${meta.pulse ? 'pulse-dot' : ''}`}
            style={{ backgroundColor: meta.color, boxShadow: meta.pulse ? `0 0 6px ${meta.color}` : 'none' }}
          />
          <span className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: meta.color }}>
            {meta.label}
          </span>
        </div>

        {/* Top-right: type badge */}
        <div className="absolute top-2 right-2 px-2 py-1 rounded-md backdrop-blur-md bg-black/40 border border-white/5">
          <span className="text-[10px] font-mono text-[var(--color-muted)]">
            {isText ? 'TEXT' : 'IMG'}
          </span>
        </div>

        {/* "NEW" flash badge */}
        {job._isNew && (
          <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 px-3 py-1 rounded-full bg-[var(--color-accent)] text-white text-[10px] font-bold uppercase tracking-widest pointer-events-none new-badge-anim">
            New
          </div>
        )}

        {/* Bottom progress bar — only when in flight */}
        {inFlight && (
          <div className="absolute bottom-0 left-0 right-0 h-1 bg-black/40">
            <div
              className="h-full bg-[var(--color-accent)] transition-all duration-500 ease-out shimmer-bar"
              style={{ width: `${Math.max(2, job.progress_pct || 0)}%` }}
            />
          </div>
        )}
      </Link>

      {/* Bottom info */}
      <div className="p-3 space-y-1.5">
        {/* Prompt or filename */}
        {isText && job.prompt ? (
          <p className="text-xs text-white italic line-clamp-2 leading-snug">"{job.prompt}"</p>
        ) : (
          <p className="text-xs text-white truncate font-medium" title={job.original_filename}>
            {job.original_filename || '—'}
          </p>
        )}

        {/* Step / message during processing */}
        {inFlight && job.current_step && (
          <p className="text-[10px] text-[var(--color-accent)] font-mono truncate">
            {job.current_step}{job.progress_pct ? ` · ${job.progress_pct}%` : ''}
          </p>
        )}

        {/* Stats for completed */}
        {job.status === 'complete' && (
          <div className="flex items-center gap-2 text-[10px] text-[var(--color-muted)] font-mono">
            {job.vertex_count != null && (
              <span>{(job.vertex_count / 1000).toFixed(0)}k v</span>
            )}
            {job.generation_time_s != null && (
              <span>{Math.round(job.generation_time_s)}s</span>
            )}
            {job.is_watertight && (
              <span className="text-[var(--color-success)]" title="Watertight">●</span>
            )}
          </div>
        )}

        {/* Error for failed */}
        {job.status === 'failed' && job.error_message && (
          <p className="text-[10px] text-[var(--color-danger)] truncate font-mono" title={job.error_message}>
            {job.error_message}
          </p>
        )}

        {/* Footer — IP + age */}
        <div className="flex items-center justify-between pt-1 border-t border-[var(--color-border)]">
          <span className="text-[10px] text-[var(--color-muted-2)] font-mono truncate">
            {job.client_ip || '—'}
          </span>
          <span className="text-[10px] text-[var(--color-muted-2)] font-mono shrink-0">
            {timeAgo(job.created_at)}
          </span>
        </div>
      </div>
    </div>
  );
}
