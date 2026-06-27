import { Blob as NodeBlob } from 'node:buffer';
import { afterEach, describe, expect, it } from 'vitest';
import { buildLiveTraceSummary, shouldUseLiveTraceSummary } from '@/recording/trace-summary';
import type { BlobRow, RecordingRow } from '@/shared/types';
import { db } from '@/storage/db';

describe('live trace summary', () => {
  afterEach(async () => {
    await db.blobs.clear();
    await db.events.clear();
    await db.recordings.clear();
  });

  it('computes current domains, duration, event counts, and media for failed recordings', async () => {
    const row = recording('tr_paused', {
      status: 'failed',
      created_at: Date.parse('2026-06-04T00:00:00.000Z'),
      updated_at: Date.parse('2026-06-04T00:00:20.000Z'),
      envelope: {
        ...recording('tr_paused').envelope,
        started_at: '2026-06-04T00:00:00.000Z',
        summary: {
          domains: [],
          duration_ms: 0,
          event_counts: {},
          screenshot_count: 0,
          video_chunk_count: 0
        }
      }
    });
    await db.recordings.put(row);
    await db.events.bulkPut([
      {
        event_id: 'ev_action',
        trace_id: 'tr_paused',
        tab_id: 1,
        timestamp: Date.parse('2026-06-04T00:00:05.000Z'),
        url: 'https://www.example.test/form',
        kind: 'action',
        action_type: 'click'
      },
      {
        event_id: 'ev_network',
        trace_id: 'tr_paused',
        tab_id: 1,
        timestamp: Date.parse('2026-06-04T00:00:08.000Z'),
        url: 'https://www.example.test/form',
        kind: 'network_request',
        request_id: 'req_1',
        method: 'POST',
        full_url: 'https://api.example.test/login',
        fetch_kind: 'fetch',
        req_headers: {}
      },
      {
        event_id: 'ev_screenshot',
        trace_id: 'tr_paused',
        tab_id: 1,
        timestamp: Date.parse('2026-06-04T00:00:12.000Z'),
        url: 'https://www.example.test/form',
        kind: 'screenshot',
        blob_key: 'blob_screen'
      }
    ]);
    await db.blobs.put({
      blob_key: 'blob_screen',
      trace_id: 'tr_paused',
      kind: 'screenshot',
      data: new NodeBlob(['png-bytes'], { type: 'image/png' }) as Blob,
      created_at: Date.parse('2026-06-04T00:00:12.000Z')
    } satisfies BlobRow);

    const summary = await buildLiveTraceSummary(row, { now: () => Date.parse('2026-06-04T00:01:00.000Z') });

    expect(summary).toEqual({
      domains: ['example.test'],
      duration_ms: 12_000,
      event_counts: {
        action: 1,
        network_request: 1,
        screenshot: 1
      },
      screenshot_count: 1,
      video_chunk_count: 0
    });
  });

  it('uses wall-clock duration for active drafts and finalized summary for stopped rows', async () => {
    const active = recording('tr_active', {
      created_at: Date.parse('2026-06-04T00:00:00.000Z'),
      updated_at: Date.parse('2026-06-04T00:00:00.000Z'),
      envelope: {
        ...recording('tr_active').envelope,
        started_at: '2026-06-04T00:00:00.000Z'
      }
    });
    const stopped = recording('tr_stopped', { status: 'uploaded' });

    expect(shouldUseLiveTraceSummary(active)).toBe(true);
    expect(shouldUseLiveTraceSummary(stopped)).toBe(false);

    const summary = await buildLiveTraceSummary(active, { now: () => Date.parse('2026-06-04T00:00:30.000Z') });

    expect(summary.duration_ms).toBe(30_000);
  });
});

function recording(traceId: string, patch: Partial<RecordingRow> = {}): RecordingRow {
  return {
    trace_id: traceId,
    status: 'recording',
    created_at: 1,
    updated_at: 1,
    envelope: {
      schema_version: 'journey_trace_v1',
      trace_id: traceId,
      recording_mode: 'research_free_form',
      started_at: '2026-06-04T00:00:00.000Z',
      tags: [],
      browser: { extension_version: '0.1.0', user_agent: 'vitest', timezone: 'UTC' },
      summary: {
        domains: [],
        duration_ms: 0,
        event_counts: {},
        screenshot_count: 0,
        video_chunk_count: 0
      }
    },
    ...patch
  };
}
