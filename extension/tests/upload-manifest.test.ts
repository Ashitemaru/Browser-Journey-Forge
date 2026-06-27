import { gunzipSync } from 'fflate';
import { Blob as NodeBlob } from 'node:buffer';
import { afterEach, describe, expect, it } from 'vitest';
import { buildUploadManifest, missingChunks, readManifestChunkPayload } from '@/upload/manifest';
import { db } from '@/storage/db';
import type { ActionEvent, BlobRow, RecordingRow } from '@/shared/types';

const traceId = 'tr_upload_manifest';

describe('upload manifest', () => {
  afterEach(async () => {
    await db.uploadManifests.clear();
    await db.blobs.clear();
    await db.events.clear();
    await db.recordings.clear();
  });

  it('chunks redacted events as gzipped NDJSON with stable hashes and recoverable payloads', async () => {
    await seedRecording();
    await db.events.bulkPut([
      actionEvent('ev_1', 1, 'email', 'shopper@example.test'),
      actionEvent('ev_2', 2, 'nickname', 'Scout')
    ]);

    const manifest = await buildUploadManifest(traceId);

    expect(manifest.chunks).toHaveLength(1);
    expect(manifest.chunks[0]).toMatchObject({ index: 0, kind: 'events', uploaded: false });
    expect(manifest.chunks[0]?.sha256).toMatch(/^[a-f0-9]{64}$/);
    expect(manifest.chunks[0]?.bytes).toBeGreaterThan(0);

    const payload = readManifestChunkPayload(manifest, 0);
    expect(payload.kind).toBe('events');
    expect(payload.contentEncoding).toBe('gzip');

    const ndjson = new TextDecoder().decode(gunzipSync(new Uint8Array(payload.body)));
    const lines = ndjson.trim().split('\n').map((line) => JSON.parse(line) as ActionEvent);
    expect(lines).toHaveLength(2);
    expect(lines[0]?.event_id).toBe('ev_1');
    expect(lines[0]?.value?.value).toBeNull();
    expect(lines[0]?.value?.redaction?.classes).toContain('classified_email');
    expect(lines[1]?.value?.value).toBe('Scout');
  });

  it('persists recoverable payload bytes through Dexie structured clone', async () => {
    await seedRecording();
    await db.events.bulkPut([
      actionEvent('ev_1', 1, 'nickname', 'Scout'),
      actionEvent('ev_2', 2, 'email', 'shopper@example.test')
    ]);

    await buildUploadManifest(traceId);
    const stored = await db.uploadManifests.get(traceId);
    if (!stored) throw new Error('expected stored manifest');

    const payload = readManifestChunkPayload(stored, 0);
    const ndjson = new TextDecoder().decode(gunzipSync(new Uint8Array(payload.body)));
    expect(ndjson).toContain('"event_id":"ev_1"');
    expect(ndjson).toContain('"event_id":"ev_2"');
    expect(ndjson).not.toContain('shopper@example.test');
  });

  it('includes media blobs as separate gzip-backed media chunks with metadata', async () => {
    await seedRecording();
    await db.events.put({
      event_id: 'ev_screenshot',
      trace_id: traceId,
      tab_id: 1,
      timestamp: 1,
      url: 'https://example.test',
      kind: 'screenshot',
      blob_key: 'blob_screen',
      width: 100,
      height: 50
    });
    const blobRow: BlobRow = {
      blob_key: 'blob_screen',
      trace_id: traceId,
      kind: 'screenshot',
      data: new NodeBlob(['image-bytes'], { type: 'image/png' }) as Blob,
      created_at: 1
    };
    await db.blobs.put(blobRow);

    const manifest = await buildUploadManifest(traceId);

    expect(manifest.chunks.map((chunk) => chunk.kind)).toEqual(['events', 'media']);
    expect(manifest.chunks[1]?.media).toEqual({
      blob_key: 'blob_screen',
      media_kind: 'screenshot',
      mime_type: 'image/png',
      created_at: 1,
      segment_index: 0,
      segment_count: 1,
      uncompressed_bytes: 11
    });
    const mediaPayload = readManifestChunkPayload(manifest, 1);
    expect(mediaPayload.kind).toBe('media');
    expect(new TextDecoder().decode(gunzipSync(new Uint8Array(mediaPayload.body)))).toBe('image-bytes');
  });

  it('splits large video blobs into bounded resumable media segments', async () => {
    await seedRecording();
    await db.blobs.put({
      blob_key: 'blob_video',
      trace_id: traceId,
      kind: 'video',
      data: new NodeBlob(['abcdefghijkl'], { type: 'video/webm' }) as Blob,
      created_at: 10
    });

    const manifest = await buildUploadManifest(traceId, {
      mediaChunkTargetBytes: 5
    });

    expect(manifest.chunks.map((chunk) => chunk.kind)).toEqual(['media', 'media', 'media']);
    expect(manifest.chunks.map((chunk) => chunk.media)).toEqual([
      {
        blob_key: 'blob_video',
        media_kind: 'video',
        mime_type: 'video/webm',
        created_at: 10,
        segment_index: 0,
        segment_count: 3,
        uncompressed_bytes: 5
      },
      {
        blob_key: 'blob_video',
        media_kind: 'video',
        mime_type: 'video/webm',
        created_at: 10,
        segment_index: 1,
        segment_count: 3,
        uncompressed_bytes: 5
      },
      {
        blob_key: 'blob_video',
        media_kind: 'video',
        mime_type: 'video/webm',
        created_at: 10,
        segment_index: 2,
        segment_count: 3,
        uncompressed_bytes: 2
      }
    ]);
    expect(
      manifest.payloads.map((payload) =>
        new TextDecoder().decode(gunzipSync(new Uint8Array(payload.body)))
      )
    ).toEqual(['abcde', 'fghij', 'kl']);
    expect(manifest.media_counts).toEqual({ screenshot: 0, video: 1 });
  });

  it('excludes media blobs marked excluded from upload', async () => {
    await seedRecording();
    await db.blobs.bulkPut([
      {
        blob_key: 'blob_keep',
        trace_id: traceId,
        kind: 'screenshot',
        data: new NodeBlob(['keep-bytes'], { type: 'image/png' }) as Blob,
        created_at: 1
      },
      {
        blob_key: 'blob_drop',
        trace_id: traceId,
        kind: 'screenshot',
        data: new NodeBlob(['drop-bytes'], { type: 'image/png' }) as Blob,
        created_at: 2,
        excluded_from_upload: true,
        excluded_at: 3
      }
    ] satisfies BlobRow[]);

    const manifest = await buildUploadManifest(traceId);

    expect(manifest.chunks.map((chunk) => chunk.kind)).toEqual(['media']);
    expect(manifest.media_counts).toEqual({ screenshot: 1, video: 0 });
    const mediaPayload = readManifestChunkPayload(manifest, 0);
    const mediaBytes = new TextDecoder().decode(gunzipSync(new Uint8Array(mediaPayload.body)));
    expect(mediaBytes).toBe('keep-bytes');
    expect(mediaBytes).not.toContain('drop-bytes');
  });

  it('returns chunks not yet accepted by the backend in manifest order', () => {
    expect(
      missingChunks(
        {
          trace_id: traceId,
          finalized: false,
          chunks: [
            { index: 0, kind: 'events', sha256: 'a', bytes: 1, uploaded: false },
            { index: 1, kind: 'media', sha256: 'b', bytes: 1, uploaded: true },
            { index: 2, kind: 'media', sha256: 'c', bytes: 1, uploaded: false }
          ]
        },
        [1]
      )
    ).toEqual([0, 2]);
  });
});

async function seedRecording(): Promise<void> {
  const row: RecordingRow = {
    trace_id: traceId,
    status: 'ready',
    created_at: 1,
    updated_at: 1,
    envelope: {
      schema_version: 'journey_trace_v1',
      trace_id: traceId,
      recording_mode: 'research_free_form',
      started_at: '2026-06-03T00:00:00.000Z',
      label: 'Test trace',
      tags: [],
      browser: { extension_version: '0.1.0', user_agent: 'vitest', timezone: 'UTC' },
      summary: {
        domains: ['example.test'],
        duration_ms: 100,
        event_counts: {},
        screenshot_count: 0,
        video_chunk_count: 0
      }
    },
    identity: {
      identity_bundle_id: 'idb_upload',
      email: 'shopper@example.test',
      email_password: 'secret-password',
      webmail_url: 'https://mail.example.test',
      persona: {},
      payment: { enabled: false },
      expires_at: '2026-06-04T00:00:00.000Z'
    }
  };
  await db.recordings.put(row);
}

function actionEvent(eventId: string, timestamp: number, name: string, value: string): ActionEvent {
  return {
    event_id: eventId,
    trace_id: traceId,
    tab_id: 1,
    timestamp,
    url: 'https://example.test/form',
    kind: 'action',
    action_type: 'input',
    target: {
      tag: 'input',
      name,
      selector: `input[name=${name}]`,
      xpath: `/html/body/input[@name="${name}"]`
    },
    value: { value }
  };
}
