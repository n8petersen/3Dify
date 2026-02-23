import { useEffect, useRef, useState, useCallback } from 'react';
import { makeWsUrl } from '../api';

const MAX_RETRIES = 10;
const BASE_DELAY = 1000;

export default function useJobWebSocket(jobId) {
  const [progress, setProgress] = useState(null);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [generatedImageUrl, setGeneratedImageUrl] = useState(null);
  const wsRef = useRef(null);
  const retriesRef = useRef(0);
  const doneRef = useRef(false);

  const connect = useCallback(() => {
    if (!jobId || doneRef.current) return;

    const ws = new WebSocket(makeWsUrl(jobId));
    wsRef.current = ws;

    ws.onopen = () => {
      retriesRef.current = 0;
    };

    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);

      if (msg.type === 'status' || msg.type === 'progress') {
        setProgress({
          step: msg.step,
          pct: msg.progress_pct,
          message: msg.message,
          status: msg.status,
        });
      } else if (msg.type === 'generated_image') {
        setGeneratedImageUrl(msg.url);
      } else if (msg.type === 'complete') {
        doneRef.current = true;
        setResult(msg);
        ws.close();
      } else if (msg.type === 'failed') {
        doneRef.current = true;
        setError({ message: msg.error, step: msg.step });
        ws.close();
      }
    };

    ws.onerror = () => {
      // Don't set error — let onclose handle reconnection
    };

    // Send keepalive pings every 25s
    const pingInterval = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send('ping');
      }
    }, 25000);

    ws.onclose = () => {
      clearInterval(pingInterval);
      // Auto-reconnect if job isn't done yet
      if (!doneRef.current && retriesRef.current < MAX_RETRIES) {
        const delay = BASE_DELAY * Math.pow(2, Math.min(retriesRef.current, 5));
        retriesRef.current += 1;
        setTimeout(connect, delay);
      }
    };

    return () => {
      clearInterval(pingInterval);
      doneRef.current = true;
      ws.close();
    };
  }, [jobId]);

  useEffect(() => {
    doneRef.current = false;
    retriesRef.current = 0;
    const cleanup = connect();
    return cleanup;
  }, [connect]);

  return { progress, result, error, generatedImageUrl };
}
