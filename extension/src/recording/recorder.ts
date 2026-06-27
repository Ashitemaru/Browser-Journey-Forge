import { getBrowserAdapter } from '@/browser';
import { collectEventDomains } from '@/shared/events';
import { createId } from '@/shared/id';
import { systemClock, type Clock } from '@/shared/time';
import type { CapturedEvent, RecordingRow, TraceSummary } from '@/shared/types';
import { db, getConfig } from '@/storage/db';

export async function startRecording(
  clock: Clock = systemClock,
  opts?: { label?: string }
): Promise<RecordingRow> {
  const config = await getConfig();
  if (!config.endpoint_url || !config.api_key) {
    throw new Error(
      'upload endpoint and API key are required before recording'
    );
  }

  const adapter = getBrowserAdapter();
  const captureSettings = {
    ...config.capture,
    screenshots: false,
    video: adapter.capabilities.video && config.capture.video,
  };
  const traceId = createId('tr_');
  const now = clock.now();
  const row: RecordingRow = {
    trace_id: traceId,
    status: 'recording',
    envelope: {
      schema_version: 'journey_trace_v1',
      trace_id: traceId,
      recording_mode: config.recording_mode,
      started_at: clock.isoNow(),
      tags: [],
      ...(resolveLabel(opts) ? { label: resolveLabel(opts) } : {}),
      capture_settings: captureSettings,
      browser: {
        extension_version: chrome.runtime.getManifest().version ?? '0.0.0',
        user_agent: navigator.userAgent,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      },
      summary: {
        domains: [],
        duration_ms: 0,
        event_counts: {},
        screenshot_count: 0,
        video_chunk_count: 0,
      },
    },
    created_at: now,
    updated_at: now,
  };

  await db.transaction('rw', db.recordings, db.events, async () => {
    await db.recordings.put(row);
    await db.events.put({
      event_id: createId('ev_'),
      trace_id: traceId,
      tab_id: -1,
      timestamp: clock.now(),
      url: '',
      kind: 'annotation',
      annotation_type: 'resume',
      text: `recording_started:${adapter.capabilities.browser}`,
    });
  });

  return row;
}

function resolveLabel(opts?: { label?: string }): string {
  return opts?.label?.trim() || '';
}

export async function stopRecording(
  traceId: string,
  clock: Clock = systemClock
): Promise<RecordingRow> {
  const now = clock.now();
  return await db.transaction('rw', db.recordings, db.events, async () => {
    const row = await db.recordings.get(traceId);
    if (!row) throw new Error(`recording not found: ${traceId}`);
    if (row.status !== 'recording') {
      throw new Error('recording must be recording before it can be stopped');
    }

    await db.events.put({
      event_id: createId('ev_'),
      trace_id: traceId,
      tab_id: -1,
      timestamp: now,
      url: '',
      kind: 'annotation',
      annotation_type: 'pause',
      text: 'recording_stopped',
    });

    const updated: RecordingRow = {
      ...row,
      status: 'ready',
      envelope: {
        ...row.envelope,
        ended_at: clock.isoNow(),
        summary: await summarizeTrace(traceId, row.envelope.started_at, now),
      },
      updated_at: now,
    };

    await db.recordings.put(updated);
    return updated;
  });
}

// Set/overwrite the task label on a stopped (`ready`) recording. Used when the
// user types a task name at upload time. No-op label values are ignored.
export async function setRecordingLabel(
  traceId: string,
  label: string,
  clock: Clock = systemClock
): Promise<RecordingRow> {
  const row = await db.recordings.get(traceId);
  if (!row) throw new Error(`recording not found: ${traceId}`);
  if (row.status !== 'ready') {
    throw new Error('recording must be ready before its label can be set');
  }

  const trimmed = label.trim();
  if (!trimmed) return row;

  const updated: RecordingRow = {
    ...row,
    envelope: { ...row.envelope, label: trimmed },
    updated_at: clock.now(),
  };
  await db.recordings.put(updated);
  return updated;
}

export async function appendEvent(event: CapturedEvent): Promise<void> {
  await db.transaction('rw', db.recordings, db.events, async () => {
    const row = await db.recordings.get(event.trace_id);
    if (!row || row.status !== 'recording') return;

    await db.events.put(event);
  });
}

async function summarizeTrace(
  traceId: string,
  startedAt: string,
  endedAt: number
): Promise<TraceSummary> {
  const events = await db.events.where('trace_id').equals(traceId).toArray();
  const domains = new Set<string>();
  const eventCounts: Record<string, number> = {};
  let screenshotCount = 0;
  let videoChunkCount = 0;

  for (const event of events) {
    eventCounts[event.kind] = (eventCounts[event.kind] ?? 0) + 1;
    collectEventDomains(domains, event);
    if (event.kind === 'screenshot') screenshotCount += 1;
    if (event.kind === 'video_chunk') videoChunkCount += 1;
  }

  return {
    domains: [...domains].sort(),
    duration_ms: Math.max(0, endedAt - Date.parse(startedAt)),
    event_counts: eventCounts,
    screenshot_count: screenshotCount,
    video_chunk_count: videoChunkCount,
  };
}
