import { useEffect, useState, useRef } from 'react';
import { useParams, Link } from 'react-router-dom';
import useJobWebSocket from '../hooks/useJobWebSocket';
import ProgressView from '../components/ProgressView';
import ResultsView from '../components/ResultsView';
import { useToast } from '../components/Toast';
import { getJob, getGeneratedImageUrl } from '../api';

export default function JobPage() {
  const { jobId } = useParams();
  const { progress, result, error: wsError, generatedImageUrl } = useJobWebSocket(jobId);
  const [job, setJob] = useState(null);
  const [pollError, setPollError] = useState(null);
  const toast = useToast();
  const pollRef = useRef(null);

  // Fetch initial job state
  useEffect(() => {
    if (!jobId) return;
    getJob(jobId)
      .then(setJob)
      .catch((err) => {
        setPollError(err.message);
        toast.error(err.message);
      });
  }, [jobId]);

  // When WS reports complete, re-fetch the full job data
  useEffect(() => {
    if (result && jobId) {
      getJob(jobId).then(setJob);
    }
  }, [result, jobId]);

  // Polling fallback — if WS isn't delivering updates, poll every 5s
  useEffect(() => {
    if (!jobId) return;

    pollRef.current = setInterval(() => {
      getJob(jobId)
        .then((data) => {
          setJob(data);
          // Stop polling once job is terminal
          if (data.status === 'complete' || data.status === 'failed') {
            clearInterval(pollRef.current);
          }
        })
        .catch(() => {});
    }, 5000);

    return () => clearInterval(pollRef.current);
  }, [jobId]);

  // Determine what to show — only the actual job status matters, not WS errors
  const isComplete = job?.status === 'complete';
  const isFailed = job?.status === 'failed' || wsError;
  const isProcessing = !isComplete && !isFailed;

  // Update job progress from WS (preferred) or polling
  const currentStep = progress?.step || job?.current_step;
  const currentPct = progress?.pct ?? job?.progress_pct ?? 0;
  const currentMessage = progress?.message || job?.progress_message;
  const queuePosition = job?.queue_position;
  const jobType = job?.job_type || 'image';

  // Generated image URL — from WS message or from job data
  const genImageUrl = generatedImageUrl || job?.generated_image_url;

  if (pollError) {
    return (
      <div className="min-h-[calc(100vh-4rem)] flex items-center justify-center px-4">
        <div className="text-center space-y-4 page-enter">
          <p className="text-[var(--color-danger)]">{pollError}</p>
          <Link to="/" className="inline-block text-sm text-[var(--color-muted)] hover:text-white transition-colors">
            Back to home
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-[calc(100vh-4rem)] flex items-center justify-center px-4 py-8">
      {isComplete && job ? (
        <ResultsView job={job} />
      ) : isFailed ? (
        <div className="w-full max-w-lg mx-auto text-center space-y-6 page-enter">
          <div className="glass-strong rounded-2xl p-8 space-y-4">
            <div className="mx-auto w-12 h-12 rounded-full bg-[var(--color-danger)]/10 flex items-center justify-center">
              <svg className="w-6 h-6 text-[var(--color-danger)]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </div>
            <div>
              <h2 className="text-lg font-semibold">Generation Failed</h2>
              <p className="text-sm text-[var(--color-muted)] mt-1">
                {wsError?.message || job?.error || 'An unexpected error occurred'}
              </p>
              {(wsError?.step || job?.error_step) && (
                <p className="text-xs text-[var(--color-muted-2)] mt-1 font-mono">
                  Failed at: {wsError?.step || job?.error_step}
                </p>
              )}
            </div>
            <p className="text-xs text-[var(--color-muted-2)]">
              Try again — the GPU may have been busy or the image format unsupported.
            </p>
          </div>
          <Link
            to="/"
            className="inline-block px-6 py-2.5 rounded-xl btn-accent text-sm font-medium"
          >
            Try again
          </Link>
        </div>
      ) : (
        <div className="w-full max-w-xl mx-auto space-y-6">
          {/* Show prompt for text jobs */}
          {jobType === 'text' && job?.prompt && (
            <div className="glass-strong rounded-xl px-4 py-3 text-center">
              <p className="text-xs text-[var(--color-muted)] mb-1">Prompt</p>
              <p className="text-sm text-white italic">"{job.prompt}"</p>
            </div>
          )}

          {/* Show generated image when available */}
          {genImageUrl && (
            <div className="glass-strong rounded-xl p-4">
              <p className="text-xs text-[var(--color-muted)] mb-2 text-center">Generated reference image</p>
              <img
                src={genImageUrl}
                alt="AI-generated reference"
                className="w-72 h-72 sm:w-80 sm:h-80 mx-auto rounded-xl object-cover border border-[var(--color-border)]"
              />
            </div>
          )}

          <ProgressView
            step={currentStep}
            pct={currentPct}
            message={currentMessage}
            queuePosition={queuePosition}
            jobType={jobType}
          />
        </div>
      )}
    </div>
  );
}
