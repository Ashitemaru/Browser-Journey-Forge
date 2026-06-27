import { collectEventDomains } from '@/shared/events';
import type { CapturedEvent, RecordingRow, TraceSummary } from '@/shared/types';
import { db } from '@/storage/db';

export function shouldUseLiveTraceSummary(recording: RecordingRow): boolean {
  return recording.status === 'recording' || recording.status === 'failed';
}

export async function buildLiveTraceSummary(
  recording: RecordingRow,
  options: { now?: () => number } = {}
): Promise<TraceSummary> {
  const [events, blobs] = await Promise.all([
    db.events.where('trace_id').equals(recording.trace_id).toArray(),
    db.blobs.where('trace_id').equals(recording.trace_id).toArray(),
  ]);

  const domains = new Set<string>();
  const eventCounts: Record<string, number> = {};
  let screenshotEvents = 0;
  let videoChunkEvents = 0;

  for (const event of events) {
    eventCounts[event.kind] = (eventCounts[event.kind] ?? 0) + 1;
    collectEventDomains(domains, event);
    if (event.kind === 'screenshot') screenshotEvents += 1;
    if (event.kind === 'video_chunk') videoChunkEvents += 1;
  }

  const uploadableBlobs = blobs.filter((blob) => !blob.excluded_from_upload);
  const screenshotBlobs = uploadableBlobs.filter(
    (blob) => blob.kind === 'screenshot'
  ).length;
  const videoBlobs = uploadableBlobs.filter(
    (blob) => blob.kind === 'video'
  ).length;

  return {
    domains: [...domains].sort(),
    duration_ms: durationFor(
      recording,
      events,
      options.now ?? (() => Date.now())
    ),
    event_counts: eventCounts,
    screenshot_count: blobs.length > 0 ? screenshotBlobs : screenshotEvents,
    video_chunk_count: blobs.length > 0 ? videoBlobs : videoChunkEvents,
  };
}

function durationFor(
  recording: RecordingRow,
  events: CapturedEvent[],
  now: () => number
): number {
  const started = Date.parse(recording.envelope.started_at);
  if (!Number.isFinite(started)) return 0;

  const ended = recording.envelope.ended_at
    ? Date.parse(recording.envelope.ended_at)
    : NaN;
  if (Number.isFinite(ended)) return Math.max(0, ended - started);

  const latestEventTimestamp = latestEventTime(events);
  if (recording.status === 'recording') {
    return Math.max(
      0,
      Math.max(now(), latestEventTimestamp ?? started) - started
    );
  }

  const fallbackEnd =
    latestEventTimestamp ?? recording.updated_at ?? recording.created_at;
  return Math.max(0, fallbackEnd - started);
}

function latestEventTime(events: CapturedEvent[]): number | undefined {
  const timestamps = events
    .map((event) => event.timestamp)
    .filter(Number.isFinite);
  return timestamps.length > 0 ? Math.max(...timestamps) : undefined;
}
