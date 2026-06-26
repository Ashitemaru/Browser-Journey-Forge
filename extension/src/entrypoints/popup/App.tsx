import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '@/shared/errors';
import { sendRuntimeMessage } from '@/shared/runtime';
import { buildLiveTraceSummary } from '@/recording/trace-summary';
import { getConfig } from '@/storage/db';
import { Alert, Badge, Button, Card, CardContent, Input } from '@/ui/primitives';
import type { RecordingRow, TraceSummary } from '@/shared/types';

type ActiveRecordingResponse = {
  active: boolean;
  traceId: string | null;
  recovered?: boolean;
  row?: RecordingRow | null;
};

type RecordingActionResponse = {
  active: boolean;
  traceId: string | null;
  row?: RecordingRow;
};

type Stopped = {
  row: RecordingRow;
  summary: TraceSummary;
};

type View = 'idle' | 'recording' | 'stopped';

export function PopupApp() {
  const [view, setView] = useState<View>('idle');
  const [endpointReady, setEndpointReady] = useState(true);
  const [taskName, setTaskName] = useState('');
  const [activeRow, setActiveRow] = useState<RecordingRow | null>(null);
  const [liveSummary, setLiveSummary] = useState<TraceSummary | null>(null);
  const [now, setNow] = useState(Date.now());
  const [stopped, setStopped] = useState<Stopped | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const stoppedRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const [config, active] = await Promise.all([
        getConfig(),
        sendRuntimeMessage<ActiveRecordingResponse>({ type: 'get-active-recording' }),
      ]);
      if (cancelled) return;
      setEndpointReady(Boolean(config.endpoint_url.trim() && config.api_key.trim()));
      if (active.active && active.row && !stoppedRef.current) {
        setActiveRow(active.row);
        setView('recording');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Poll live trace stats + elapsed timer while recording.
  useEffect(() => {
    if (view !== 'recording') return undefined;
    let cancelled = false;
    const tick = async () => {
      setNow(Date.now());
      const active = await sendRuntimeMessage<ActiveRecordingResponse>({ type: 'get-active-recording' });
      if (cancelled) return;
      if (!active.active || !active.row) {
        setView('idle');
        setActiveRow(null);
        return;
      }
      setActiveRow(active.row);
      try {
        setLiveSummary(await buildLiveTraceSummary(active.row));
      } catch {
        // Live stats are best-effort.
      }
    };
    void tick();
    const handle = setInterval(() => void tick(), 1000);
    return () => {
      cancelled = true;
      clearInterval(handle);
    };
  }, [view]);

  async function run(action: () => Promise<void>): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  function start(): void {
    void run(async () => {
      const response = await sendRuntimeMessage<RecordingActionResponse>(
        taskName.trim() ? { type: 'start-recording', label: taskName.trim() } : { type: 'start-recording' }
      );
      setNotice(null);
      setActiveRow(response.row ?? null);
      setLiveSummary(null);
      setNow(Date.now());
      setView('recording');
    });
  }

  function stop(): void {
    void run(async () => {
      const traceId = activeRow?.trace_id;
      const response = await sendRuntimeMessage<RecordingActionResponse>(
        traceId ? { type: 'stop-recording', traceId } : { type: 'stop-recording' }
      );
      const row = response.row;
      if (!row) {
        setView('idle');
        return;
      }
      stoppedRef.current = true;
      const summary = await buildLiveTraceSummary(row);
      setStopped({ row, summary });
      setActiveRow(null);
      setView('stopped');
    });
  }

  function upload(): void {
    if (!stopped) return;
    void run(async () => {
      const traceId = stopped.row.trace_id;
      await sendRuntimeMessage<{ ok: boolean }>(
        taskName.trim()
          ? { type: 'resume-upload', traceId, label: taskName.trim() }
          : { type: 'resume-upload', traceId }
      );
      stoppedRef.current = false;
      setStopped(null);
      setTaskName('');
      setNotice('Recording uploaded.');
      setView('idle');
    });
  }

  function discard(): void {
    if (!stopped) return;
    void run(async () => {
      await sendRuntimeMessage<{ ok: boolean }>({ type: 'delete-recording', traceId: stopped.row.trace_id });
      stoppedRef.current = false;
      setStopped(null);
      setTaskName('');
      setNotice('Recording discarded.');
      setView('idle');
    });
  }

  return (
    <main className="jf-popup">
      <header className="jf-header">
        <h1 className="jf-title">Journey Forge</h1>
        <span className={view === 'recording' ? 'jf-dot recording' : 'jf-dot'} aria-hidden />
      </header>

      {error ? <Alert tone="danger">{error}</Alert> : null}

      {view === 'idle' ? (
        <>
          {notice ? <Alert tone="success">{notice}</Alert> : null}
          {!endpointReady ? (
            <Alert tone="warning">Set the local server endpoint and API key before recording.</Alert>
          ) : null}
          <label className="jf-field">
            <span>Task name (optional)</span>
            <Input
              value={taskName}
              placeholder="e.g. Book a flight to Tokyo"
              onChange={(event) => setTaskName(event.target.value)}
              disabled={busy}
            />
          </label>
          <Button variant="primary" className="jf-wide" onClick={start} disabled={busy || !endpointReady}>
            Start recording
          </Button>
        </>
      ) : null}

      {view === 'recording' ? (
        <>
          <Card>
            <CardContent>
              <div className="jf-rec-top">
                <span className="jf-muted">Recording</span>
                <Badge tone="danger">Live</Badge>
              </div>
              <div className="jf-stat-row">
                <div className="jf-stat">
                  <strong>{formatDuration(elapsedMs(activeRow, now))}</strong>
                  <span>elapsed</span>
                </div>
                <div className="jf-stat">
                  <strong>{liveSummary ? totalEvents(liveSummary) : 0}</strong>
                  <span>events</span>
                </div>
              </div>
            </CardContent>
          </Card>
          <Button variant="danger" className="jf-wide" onClick={stop} disabled={busy}>
            Stop
          </Button>
        </>
      ) : null}

      {view === 'stopped' && stopped ? (
        <>
          <Card>
            <CardContent>
              <div className="jf-rec-top">
                <span className="jf-muted">Recording saved</span>
                <Badge tone="success">Ready</Badge>
              </div>
              <div className="jf-summary-grid">
                <div className="jf-stat">
                  <strong>{totalEvents(stopped.summary)}</strong>
                  <span>events</span>
                </div>
                <div className="jf-stat">
                  <strong>{stopped.summary.domains.length}</strong>
                  <span>domains</span>
                </div>
                <div className="jf-stat">
                  <strong>{formatDuration(stopped.summary.duration_ms)}</strong>
                  <span>duration</span>
                </div>
              </div>
              {stopped.summary.domains.length ? (
                <p className="jf-domains">{stopped.summary.domains.slice(0, 6).join(', ')}</p>
              ) : null}
            </CardContent>
          </Card>
          <div className="jf-actions">
            <Button variant="primary" onClick={upload} disabled={busy}>
              Upload
            </Button>
            <Button onClick={discard} disabled={busy}>
              Discard
            </Button>
          </div>
        </>
      ) : null}
    </main>
  );
}

function elapsedMs(row: RecordingRow | null, now: number): number {
  if (!row) return 0;
  const started = Date.parse(row.envelope.started_at);
  if (!Number.isFinite(started)) return 0;
  return Math.max(0, now - started);
}

function totalEvents(summary: TraceSummary): number {
  return Object.values(summary.event_counts).reduce((sum, count) => sum + count, 0);
}

function formatDuration(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, '0')}`;
}
