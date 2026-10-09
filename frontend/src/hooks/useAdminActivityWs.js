import { useEffect, useRef, useState, useCallback } from 'react';
import { makeAdminActivityWsUrl } from '../api';

/**
 * Live admin activity feed.
 *
 * Maintains a rolling list of jobs (most recent first) and folds in:
 *   - snapshot          → initial fill
 *   - job_created       → prepend (with `_isNew` flag for slide-in animation)
 *   - job_assigned      → status: assigned
 *   - job_progress      → progress_pct + current_step
 *   - job_generated_image → generated_image_url
 *   - job_complete      → status: complete + glb/stl URLs + metrics
 *   - job_failed        → status: failed + error
 *
 * Auto-reconnects with exponential backoff (max 30s).
 */
export default function useAdminActivityWs({ maxJobs = 100 } = {}) {
  const [jobs, setJobs] = useState([]);
  const [connected, setConnected] = useState(false);
  const [workerConnected, setWorkerConnected] = useState(false);
  const [workerBackend, setWorkerBackend] = useState('local');
  const wsRef = useRef(null);
  const reconnectRef = useRef(null);
  const backoffRef = useRef(1000);

  const updateJob = useCallback((jobId, patch) => {
    setJobs((prev) => prev.map((j) => (j.id === jobId ? { ...j, ...patch } : j)));
  }, []);

  const connect = useCallback(() => {
    const url = makeAdminActivityWsUrl();
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      backoffRef.current = 1000;
    };

    ws.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch { return; }

      switch (msg.type) {
        case 'snapshot':
          setJobs((msg.jobs || []).slice(0, maxJobs));
          setWorkerConnected(!!msg.worker_connected);
          setWorkerBackend(msg.backend || 'local');
          break;

        case 'job_created':
          setJobs((prev) => {
            const incoming = { ...msg.job, _isNew: true, _seenAt: Date.now() };
            const filtered = prev.filter((j) => j.id !== incoming.id);
            return [incoming, ...filtered].slice(0, maxJobs);
          });
          break;

        case 'job_assigned':
          updateJob(msg.job_id, { status: 'assigned' });
          break;

        case 'job_progress':
          updateJob(msg.job_id, {
            status: 'processing',
            progress_pct: msg.progress_pct ?? 0,
            current_step: msg.step,
            progress_message: msg.message,
          });
          break;

        case 'job_generated_image':
          updateJob(msg.job_id, { generated_image_url: msg.url });
          break;

        case 'job_complete':
          updateJob(msg.job_id, {
            status: 'complete',
            vertex_count: msg.vertex_count,
            face_count: msg.face_count,
            is_watertight: msg.is_watertight,
            generation_time_s: msg.generation_time_s,
            glb_url: msg.glb_url,
            stl_url: msg.stl_url,
            progress_pct: 100,
            completed_at: msg.ts,
            _justCompleted: true,
          });
          // Clear the just-completed flash after a moment
          setTimeout(() => updateJob(msg.job_id, { _justCompleted: false }), 1800);
          break;

        case 'job_failed':
          updateJob(msg.job_id, {
            status: 'failed',
            error_message: msg.error,
            error_step: msg.step,
            completed_at: msg.ts,
          });
          break;

        case 'ping':
          // Server heartbeat — no-op
          break;
        default:
          break;
      }
    };

    ws.onclose = () => {
      setConnected(false);
      // Exponential backoff reconnect
      const delay = Math.min(backoffRef.current, 30_000);
      backoffRef.current = Math.min(delay * 2, 30_000);
      reconnectRef.current = setTimeout(connect, delay);
    };

    ws.onerror = () => {
      try { ws.close(); } catch (_) { /* ignore */ }
    };
  }, [maxJobs, updateJob]);

  useEffect(() => {
    connect();
    return () => {
      if (reconnectRef.current) clearTimeout(reconnectRef.current);
      if (wsRef.current) {
        wsRef.current.onclose = null;
        try { wsRef.current.close(); } catch (_) { /* ignore */ }
      }
    };
  }, [connect]);

  return { jobs, connected, workerConnected, workerBackend };
}
