import { useState, useEffect } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from '../hooks/useAuth';
import { getMyJobs } from '../api';

const STATUS_COLORS = {
  pending: 'text-[var(--color-warning)]',
  assigned: 'text-[var(--color-warning)]',
  processing: 'text-[var(--color-accent)]',
  complete: 'text-[var(--color-success)]',
  failed: 'text-[var(--color-danger)]',
  expired: 'text-[var(--color-muted-2)]',
};

function JobCard({ job }) {
  return (
    <Link
      to={`/job/${job.job_id}`}
      className="glass-strong rounded-xl overflow-hidden hover:border-[var(--color-accent)]/30 transition-all group"
    >
      <div className="aspect-square bg-[var(--color-surface-2)] relative overflow-hidden">
        {job.thumbnail_url ? (
          <img
            src={job.thumbnail_url}
            alt={job.original_filename}
            className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300"
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center text-[var(--color-muted-2)]">
            <svg className="w-8 h-8" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="m2.25 15.75 5.159-5.159a2.25 2.25 0 0 1 3.182 0l5.159 5.159m-1.5-1.5 1.409-1.409a2.25 2.25 0 0 1 3.182 0l2.909 2.909M3.75 21h16.5A2.25 2.25 0 0 0 22.5 18.75V5.25A2.25 2.25 0 0 0 20.25 3H3.75A2.25 2.25 0 0 0 1.5 5.25v13.5A2.25 2.25 0 0 0 3.75 21Z" />
            </svg>
          </div>
        )}
        <span className={`absolute top-2 right-2 px-2 py-0.5 rounded-full text-xs font-medium bg-[var(--color-bg)]/80 backdrop-blur ${STATUS_COLORS[job.status] || ''}`}>
          {job.status}
        </span>
      </div>
      <div className="p-3 space-y-1">
        <p className="text-sm font-medium truncate">{job.original_filename}</p>
        <div className="flex items-center justify-between text-xs text-[var(--color-muted-2)]">
          <span>{new Date(job.created_at).toLocaleDateString()}</span>
          {job.vertex_count && (
            <span className="font-mono">{(job.vertex_count / 1000).toFixed(0)}K verts</span>
          )}
        </div>
      </div>
    </Link>
  );
}

export default function MyJobs() {
  const { user, loading: authLoading } = useAuth();
  const navigate = useNavigate();
  const [jobs, setJobs] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pages, setPages] = useState(1);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (authLoading) return;
    if (!user) {
      navigate('/', { replace: true });
      return;
    }

    setLoading(true);
    getMyJobs(page)
      .then((data) => {
        if (!data) { navigate('/', { replace: true }); return; }
        setJobs(data.jobs);
        setTotal(data.total);
        setPages(data.pages);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [user, authLoading, page, navigate]);

  if (authLoading || loading) {
    return (
      <div className="max-w-6xl mx-auto px-4 py-12">
        <div className="flex items-center justify-center py-24">
          <div className="w-6 h-6 border-2 border-[var(--color-accent)] border-t-transparent rounded-full animate-spin" />
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-6xl mx-auto px-4 py-12 page-enter">
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-2xl font-bold">My Jobs</h1>
          <p className="text-sm text-[var(--color-muted-2)] mt-1">
            {total} {total === 1 ? 'job' : 'jobs'} total
          </p>
        </div>
        <Link to="/" className="btn-accent px-4 py-2 rounded-lg text-sm font-medium">
          New upload
        </Link>
      </div>

      {jobs.length === 0 ? (
        <div className="glass-strong rounded-2xl p-12 text-center">
          <p className="text-lg text-[var(--color-muted)]">No jobs yet</p>
          <p className="text-sm text-[var(--color-muted-2)] mt-2">
            Upload a photo and your jobs will appear here.
          </p>
          <Link to="/" className="inline-block mt-6 btn-accent px-6 py-2.5 rounded-lg text-sm font-medium">
            Upload your first photo
          </Link>
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-4">
            {jobs.map((job) => <JobCard key={job.job_id} job={job} />)}
          </div>

          {pages > 1 && (
            <div className="flex items-center justify-center gap-2 mt-8">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page === 1}
                className="px-3 py-1.5 rounded-lg text-sm border border-[var(--color-border-2)] text-[var(--color-muted)] hover:text-white disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
              >
                Previous
              </button>
              <span className="text-sm text-[var(--color-muted-2)]">
                Page {page} of {pages}
              </span>
              <button
                onClick={() => setPage((p) => Math.min(pages, p + 1))}
                disabled={page === pages}
                className="px-3 py-1.5 rounded-lg text-sm border border-[var(--color-border-2)] text-[var(--color-muted)] hover:text-white disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
              >
                Next
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
