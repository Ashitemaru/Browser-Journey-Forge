import { Blob as NodeBlob } from 'node:buffer';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { uploadRecording } from '@/upload/runner';
import { buildUploadManifest } from '@/upload/manifest';
import { db, DEFAULT_CONFIG } from '@/storage/db';
import type { BlobRow, RecordingRow } from '@/shared/types';
import type { UploadManifestWithPayload } from '@/upload/manifest';

const traceId = 'tr_upload_runner';
const endpointUrl = 'https://api.example.test';
const apiKey = 'runner-api-key';

describe('upload runner', () => {
  afterEach(async () => {
    vi.unstubAllGlobals();
    await db.uploadManifests.clear();
    await db.blobs.clear();
    await db.events.clear();
    await db.recordings.clear();
    await db.config.clear();
  });

  it('uploads backend missing chunks plus locally unaccepted chunks before finalizing', async () => {
    await seedRecording();
    await seedManifest([0, 1, 2, 3]);
    const fetchMock = stubUploadFetch({
      initAccepted: [0],
      statusAccepted: [0],
      statusMissing: [2]
    });

    const row = await uploadRecording(traceId);

    expect(row.status).toBe('uploaded');
    const uploadPaths = chunkUploadPaths(fetchMock);
    expect(uploadPaths).toEqual([
      '/v1/traces/upl_runner/chunks/1',
      '/v1/traces/upl_runner/chunks/2',
      '/v1/traces/upl_runner/chunks/3'
    ]);
    expect(finalizeCallIndex(fetchMock)).toBeGreaterThan(chunkCallIndex(fetchMock, 3));
    expect((await db.uploadManifests.get(traceId))?.finalized).toBe(true);
  });

  it('resumes missing video media segments by manifest chunk index', async () => {
    await seedRecording();
    await db.blobs.put({
      blob_key: 'blob_video',
      trace_id: traceId,
      kind: 'video',
      data: new NodeBlob(['abcdefghijkl'], { type: 'video/webm' }) as Blob,
      created_at: 1
    });
    const manifest = await buildUploadManifest(traceId, {
      mediaChunkTargetBytes: 5
    });
    expect(manifest.chunks.map((chunk) => chunk.media?.segment_index)).toEqual([0, 1, 2]);
    const fetchMock = stubUploadFetch({
      initAccepted: [0],
      statusAccepted: [0],
      statusMissing: [2]
    });

    await uploadRecording(traceId);

    expect(chunkUploadPaths(fetchMock)).toEqual([
      '/v1/traces/upl_runner/chunks/1',
      '/v1/traces/upl_runner/chunks/2'
    ]);
    expect((await db.uploadManifests.get(traceId))?.chunks.map((chunk) => chunk.uploaded)).toEqual([true, true, true]);
  });

  it('marks recording failed and leaves trace data when a chunk response mismatches local state', async () => {
    await seedRecording();
    await seedManifest([0, 1]);
    stubUploadFetch({
      initAccepted: [0],
      statusAccepted: [0],
      statusMissing: [],
      chunkResponse: (_index, sha256) => ({ ok: true, chunk_index: 1, sha256: `${sha256}-wrong` })
    });

    await expect(uploadRecording(traceId)).rejects.toThrow(/chunk 1 response mismatch/);

    const row = await db.recordings.get(traceId);
    const manifest = await db.uploadManifests.get(traceId);
    expect(row?.status).toBe('failed');
    expect(row?.last_error).toMatch(/chunk 1 response mismatch/);
    expect(manifest?.chunks.find((chunk) => chunk.index === 1)?.uploaded).toBe(false);
    expect(await db.recordings.get(traceId)).toBeTruthy();
    expect(await db.uploadManifests.get(traceId)).toBeTruthy();
  });

  it.each([
    ['trace id', { status: 'uploaded', trace_id: 'tr_other' }],
    ['status', { status: '', trace_id: traceId }]
  ])('rejects finalize %s mismatches before marking finalized', async (_label, finalizeBody) => {
    await seedRecording();
    await seedManifest([0]);
    stubUploadFetch({
      initAccepted: [0],
      statusAccepted: [0],
      statusMissing: [],
      finalizeBody
    });

    await expect(uploadRecording(traceId)).rejects.toThrow(/finalize response mismatch/);

    const row = await db.recordings.get(traceId);
    const manifest = await db.uploadManifests.get(traceId);
    expect(row?.status).toBe('failed');
    expect(manifest?.finalized).toBe(false);
    expect(await db.recordings.get(traceId)).toBeTruthy();
  });

});

async function seedRecording(): Promise<void> {
  await db.config.put({
    ...DEFAULT_CONFIG,
    endpoint_url: endpointUrl,
    api_key: apiKey
  });
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
      label: 'Runner trace',
      tags: ['runner'],
      browser: { extension_version: '0.1.0', user_agent: 'vitest', timezone: 'UTC' },
      summary: {
        domains: [],
        duration_ms: 0,
        event_counts: {},
        screenshot_count: 0,
        video_chunk_count: 0
      }
    }
  };
  await db.recordings.put(row);
}

async function seedManifest(indexes: number[]): Promise<UploadManifestWithPayload> {
  await db.events.put({
    event_id: 'ev_runner',
    trace_id: traceId,
    tab_id: 1,
    timestamp: 1,
    url: 'https://example.test',
    kind: 'annotation',
    annotation_type: 'label_updated',
    text: 'runner test'
  });
  const mediaCount = Math.max(0, indexes.length - 1);
  const blobs: BlobRow[] = Array.from({ length: mediaCount }, (_, offset) => ({
    blob_key: `blob_${offset + 1}`,
    trace_id: traceId,
    kind: 'screenshot',
    data: new NodeBlob([`media-${offset + 1}`], { type: 'image/png' }) as Blob,
    created_at: offset + 1
  }));
  if (blobs.length > 0) {
    await db.blobs.bulkPut(blobs);
  }
  const manifest = await buildUploadManifest(traceId);
  expect(manifest.chunks.map((chunk) => chunk.index)).toEqual(indexes);
  return manifest;
}

function stubUploadFetch(opts: {
  initAccepted: number[];
  statusAccepted: number[];
  statusMissing: number[];
  chunkResponse?: (index: number, sha256: string) => { ok: boolean; chunk_index: number; sha256: string };
  finalizeBody?: { status: string; trace_id: string };
}): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    const method = init?.method;
    if (method === 'POST' && url.pathname === '/v1/traces/init') {
      return jsonResponse({ upload_id: 'upl_runner', accepted_chunks: opts.initAccepted, status: 'initialized' });
    }
    if (method === 'GET' && url.pathname === '/v1/traces/upl_runner/status') {
      return jsonResponse({
        status: 'uploading',
        accepted_chunks: opts.statusAccepted,
        missing_chunks: opts.statusMissing
      });
    }
    const chunkMatch = url.pathname.match(/^\/v1\/traces\/upl_runner\/chunks\/(\d+)$/);
    if (method === 'PUT' && chunkMatch) {
      const index = Number(chunkMatch[1]);
      const sha256 = String((init?.headers as Record<string, string>)['X-Trace-Chunk-Sha256']);
      return jsonResponse(opts.chunkResponse?.(index, sha256) ?? { ok: true, chunk_index: index, sha256 });
    }
    if (method === 'POST' && url.pathname === '/v1/traces/upl_runner/finalize') {
      return jsonResponse(opts.finalizeBody ?? { status: 'processing', trace_id: traceId });
    }
    return jsonResponse({ error: `unexpected request ${method ?? 'GET'} ${url.pathname}` }, 500);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function chunkUploadPaths(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls
    .filter(([, init]) => init?.method === 'PUT')
    .map(([input]) => new URL(String(input)).pathname);
}

function chunkCallIndex(fetchMock: ReturnType<typeof vi.fn>, chunkIndex: number): number {
  return fetchMock.mock.calls.findIndex(
    ([input, init]) => init?.method === 'PUT' && new URL(String(input)).pathname.endsWith(`/chunks/${chunkIndex}`)
  );
}

function finalizeCallIndex(fetchMock: ReturnType<typeof vi.fn>): number {
  return fetchMock.mock.calls.findIndex(
    ([input, init]) => init?.method === 'POST' && new URL(String(input)).pathname.endsWith('/finalize')
  );
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' }
  });
}
