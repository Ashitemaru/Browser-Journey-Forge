import { describe, expect, it } from 'vitest';
import {
  buildUploadQueueStats,
  uploadQueueChunkDetail,
  uploadQueueChunkLabel,
} from '@/upload/queue';
import type { RecordingRow } from '@/shared/types';

describe('upload queue model', () => {
  it('summarizes pending, retry, and media chunks from a saved manifest', () => {
    const stats = buildUploadQueueStats(recording({ status: 'failed', last_error: 'chunk 2 failed' }), {
      trace_id: 'tr_queue',
      finalized: false,
      chunks: [
        { index: 0, kind: 'events', sha256: 'a', bytes: 100, uploaded: true },
        { index: 1, kind: 'media', sha256: 'b', bytes: 200, uploaded: true },
        { index: 2, kind: 'media', sha256: 'c', bytes: 300, uploaded: false },
        { index: 3, kind: 'events', sha256: 'd', bytes: 400, uploaded: false },
      ],
    });

    expect(stats).toEqual({
      totalChunks: 4,
      uploadedChunks: 2,
      pendingChunks: 2,
      retryChunks: 2,
      mediaChunks: 2,
      pendingMediaChunks: 1,
      eventChunks: 2,
      totalBytes: 1000,
      uploadedBytes: 300,
      pendingBytes: 700,
      finalized: false,
      hasManifest: true,
      lastError: 'chunk 2 failed',
    });
    expect(uploadQueueChunkLabel(stats)).toBe('2/4 chunks uploaded');
    expect(uploadQueueChunkDetail(stats)).toContain('2 retry chunks');
    expect(uploadQueueChunkDetail(stats)).toContain('1/2 media pending');
  });

  it('describes ready recordings before a manifest exists', () => {
    const stats = buildUploadQueueStats(recording({ status: 'ready' }), undefined);

    expect(stats.hasManifest).toBe(false);
    expect(uploadQueueChunkLabel(stats)).toBe('No manifest yet');
    expect(uploadQueueChunkDetail(stats)).toBe('Manifest will be built when upload starts.');
  });
});

function recording(patch: Partial<RecordingRow> = {}): RecordingRow {
  return {
    trace_id: 'tr_queue',
    status: 'ready',
    created_at: 1,
    updated_at: 2,
    envelope: {
      schema_version: 'journey_trace_v1',
      trace_id: 'tr_queue',
      recording_mode: 'research_free_form',
      started_at: '2026-06-05T00:00:00.000Z',
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
