import { useMemo, useState, useEffect, useRef } from 'react';
import useAdminActivityWs from '../../hooks/useAdminActivityWs';
import ActivityCard from '../../components/admin/ActivityCard';

const FILTERS = [
  { id: 'all',     label: 'All' },
  { id: 'live',    label: 'Live' },
  { id: 'image',   label: 'Image' },
  { id: 'text',    label: 'Text' },
  { id: 'failed',  label: 'Failed' },
];

const LIVE_STATUSES = new Set(['pending', 'assigned', 'processing']);

function StatPill({ label, value, accent, dot }) {
  return (
    <div className="flex items-center gap-2 px-3 py-2 rounded-xl glass-strong border border-[var(--color-border)]">
      {dot && (
        <span className="w-1.5 h-1.5 rounded-full pulse-dot" style={{ backgroundColor: accent ? 'var(--color-accent)' : 'var(--color-muted)', boxShadow: `0 0 6px ${accent ? 'var(--color-accent)' : 'transparent'}` }} />
      )}
      <span className="text-[10px] uppercase tracking-widest text-[var(--color-muted)]">{label}</span>
      <span className={`text-sm font-mono font-bold ${accent ? 'text-[var(--color-accent)]' : 'text-white'}`}>{value}</span>
    </div>
  );
}

export default function AdminActivity() {
  const { jobs, connected, workerConnected } = useAdminActivityWs({ maxJobs: 120 });
  const [filter, setFilter] = useState('all');
  const [throughput, setThroughput] = useState(0);
  const completedRef = useRef([]);

  // Track completed timestamps over the last 5 min for throughput
  useEffect(() => {
    const completed = jobs.filter((j) => j.status === 'complete' && j.completed_at);
    completedRef.current = completed
      .map((j) => new Date(j.completed_at).getTime())
      .filter((t) => !Number.isNaN(t));
    const fiveMinAgo = Date.now() - 5 * 60_000;
    const recent = completedRef.current.filter((t) => t >= fiveMinAgo);
    setThroughput(recent.length);
  }, [jobs]);

  const filtered = useMemo(() => {
    switch (filter) {
      case 'live':   return jobs.filter((j) => LIVE_STATUSES.has(j.status));
      case 'image':  return jobs.filter((j) => j.job_type !== 'text');
      case 'text':   return jobs.filter((j) => j.job_type === 'text');
      case 'failed': return jobs.filter((j) => j.status === 'failed');
      default:       return jobs;
    }
  }, [jobs, filter]);

  const liveCount = useMemo(
    () => jobs.filter((j) => LIVE_STATUSES.has(j.status)).length,
    [jobs]
  );
  const completedCount = useMemo(
    () => jobs.filter((j) => j.status === 'complete').length,
    [jobs]
  );
  const failedCount = useMemo(
    () => jobs.filter((j) => j.status === 'failed').length,
    [jobs]
  );

  return (
    <div className="max-w-7xl space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold flex items-center gap-3">
            Activity
            <span className={`flex items-center gap-1.5 text-[10px] font-mono uppercase tracking-widest px-2 py-1 rounded-md ${connected ? 'text-[var(--color-success)] bg-[var(--color-success)]/5' : 'text-[var(--color-danger)] bg-[var(--color-danger)]/5'}`}>
              <span className={`w-1.5 h-1.5 rounded-full ${connected ? 'pulse-dot' : ''}`} style={{ backgroundColor: connected ? 'var(--color-success)' : 'var(--color-danger)' }} />
              {connected ? 'Live' : 'Reconnecting…'}
            </span>
          </h1>
          <p className="text-xs text-[var(--color-muted-2)] mt-1">Real-time view of every job in flight and recently completed.</p>
        </div>

        {/* Stat pills */}
        <div className="flex items-center gap-2 flex-wrap">
          <StatPill label="Live" value={liveCount} accent dot />
          <StatPill label="Done" value={completedCount} />
          <StatPill label="Failed" value={failedCount} />
          <StatPill label="5m" value={`${throughput}/5m`} />
          <StatPill label="Worker" value={workerConnected ? 'Online' : 'Offline'} />
        </div>
      </div>

      {/* Filters */}
      <div className="flex items-center gap-1">
        {FILTERS.map((f) => {
          const active = filter === f.id;
          const count = f.id === 'all'    ? jobs.length
                      : f.id === 'live'   ? liveCount
                      : f.id === 'image'  ? jobs.filter((j) => j.job_type !== 'text').length
                      : f.id === 'text'   ? jobs.filter((j) => j.job_type === 'text').length
                      : failedCount;
          return (
            <button
              key={f.id}
              onClick={() => setFilter(f.id)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors flex items-center gap-2 ${
                active
                  ? 'bg-[var(--color-accent)]/10 text-[var(--color-accent)]'
                  : 'text-[var(--color-muted)] hover:text-white hover:bg-white/5'
              }`}
            >
              {f.label}
              <span className={`text-[10px] font-mono ${active ? 'text-[var(--color-accent)]' : 'text-[var(--color-muted-2)]'}`}>
                {count}
              </span>
            </button>
          );
        })}
      </div>

      {/* Grid */}
      {filtered.length === 0 ? (
        <div className="glass-strong rounded-2xl p-12 text-center">
          <p className="text-sm text-[var(--color-muted)]">
            {jobs.length === 0 ? 'Waiting for activity…' : 'No jobs match this filter.'}
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-4">
          {filtered.map((job) => (
            <ActivityCard key={job.id} job={job} />
          ))}
        </div>
      )}
    </div>
  );
}
