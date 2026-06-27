import {
  finalizeTraceUpload,
  getTraceUploadStatus,
  initTraceUpload,
  uploadChunk,
  type TraceUploadStatus
} from '@/upload/client';
import {
  buildUploadManifest,
  missingChunks,
  readManifestChunkPayload,
  type UploadManifestWithPayload
} from '@/upload/manifest';
import type { RecordingRow, RecordingStatus, UploadManifest } from '@/shared/types';
import { db, getConfig } from '@/storage/db';

const RESUMABLE_STATUSES: RecordingStatus[] = ['ready', 'failed', 'uploading'];

export async function uploadNextRecording(signal?: AbortSignal): Promise<RecordingRow | null> {
  const candidates = await db.recordings.where('status').anyOf(RESUMABLE_STATUSES).toArray();
  const next = candidates.sort((a, b) => a.updated_at - b.updated_at || a.created_at - b.created_at)[0];
  if (!next) return null;
  return await uploadRecording(next.trace_id, signal);
}

export async function uploadRecording(traceId: string, signal?: AbortSignal): Promise<RecordingRow> {
  const row = await db.recordings.get(traceId);
  if (!row) throw new Error(`recording not found: ${traceId}`);
  if (!RESUMABLE_STATUSES.includes(row.status)) {
    throw new Error(`recording cannot be uploaded from status: ${row.status}`);
  }

  await markRecordingUploading(row);

  try {
    const config = await getConfig();
    if (!config.endpoint_url || !config.api_key) {
      throw new Error('upload endpoint and API key are required before upload');
    }

    const freshRow = await requireRecording(traceId);
    let manifest = await loadPayloadManifest(traceId);
    const init = await initTraceUpload({
      endpointUrl: config.endpoint_url,
      apiKey: config.api_key,
      signal,
      trace: {
        trace_id: freshRow.envelope.trace_id,
        schema_version: freshRow.envelope.schema_version,
        recording_mode: freshRow.envelope.recording_mode,
        label: freshRow.envelope.label ?? '',
        description: freshRow.envelope.description ?? '',
        tags: freshRow.envelope.tags,
        summary: freshRow.envelope.summary,
        capture_settings: freshRow.envelope.capture_settings ?? config.capture
      }
    });

    manifest = await persistUploadId(traceId, manifest, init.uploadId);
    const status = await getTraceUploadStatus({
      endpointUrl: config.endpoint_url,
      apiKey: config.api_key,
      signal,
      uploadId: init.uploadId
    }).catch<TraceUploadStatus>((error) => {
      console.warn(`[journey-forge] upload status check failed, using init status: ${String(error)}`);
      return {
        status: init.status,
        missingChunks: [],
        acceptedChunks: init.acceptedChunks
      };
    });

    const accepted = uniqueNumbers([...init.acceptedChunks, ...status.acceptedChunks]);
    markAccepted(manifest, accepted);
    await db.uploadManifests.put(manifest);

    const candidateMissing = uniqueNumbers([...status.missingChunks, ...missingChunks(manifest, accepted)]);
    for (const chunkIndex of candidateMissing) {
      const chunk = manifest.chunks.find((candidate) => candidate.index === chunkIndex);
      if (!chunk) continue;

      const payload = readManifestChunkPayload(manifest, chunkIndex);
      const uploaded = await uploadChunk({
        endpointUrl: config.endpoint_url,
        apiKey: config.api_key,
        signal,
        uploadId: init.uploadId,
        chunkIndex,
        kind: chunk.kind,
        sha256: chunk.sha256,
        body: payload.body
      });
      validateChunkUploadResponse(chunkIndex, chunk.sha256, uploaded);

      chunk.uploaded = true;
      await db.uploadManifests.put(manifest);
    }

    const finalized = await finalizeTraceUpload({
      endpointUrl: config.endpoint_url,
      apiKey: config.api_key,
      signal,
      uploadId: init.uploadId,
      manifest
    });
    validateFinalizeResponse(traceId, finalized);

    manifest.finalized = true;
    await db.uploadManifests.put(manifest);

    // The local server ingests + distills; there is no judge. Once finalize
    // succeeds the recording is terminally `uploaded`.
    return await updateRecording(traceId, {
      status: 'uploaded',
      upload_id: init.uploadId
    });
  } catch (error) {
    await updateRecording(traceId, {
      status: 'failed',
      last_error: error instanceof Error ? error.message : String(error)
    });
    throw error;
  }
}

async function loadPayloadManifest(traceId: string): Promise<UploadManifestWithPayload> {
  const stored = (await db.uploadManifests.get(traceId)) as UploadManifestWithPayload | undefined;
  if (stored?.payloads?.length) return stored;
  return await buildUploadManifest(traceId);
}

async function persistUploadId(
  traceId: string,
  manifest: UploadManifestWithPayload,
  uploadId: string
): Promise<UploadManifestWithPayload> {
  const updatedManifest = { ...manifest, upload_id: uploadId };
  await db.uploadManifests.put(updatedManifest);

  const row = await requireRecording(traceId);
  await updateRecording(traceId, { ...row, status: 'uploading', upload_id: uploadId });

  return updatedManifest;
}

function markAccepted(manifest: UploadManifest, acceptedIndexes: number[]): void {
  const accepted = new Set(acceptedIndexes);
  for (const chunk of manifest.chunks) {
    if (accepted.has(chunk.index)) chunk.uploaded = true;
  }
}

async function markRecordingUploading(row: RecordingRow): Promise<void> {
  const { last_error: _lastError, ...rest } = row;
  await db.recordings.put({
    ...rest,
    status: 'uploading',
    updated_at: Date.now()
  });
}

async function updateRecording(traceId: string, patch: Partial<RecordingRow>): Promise<RecordingRow> {
  const row = await requireRecording(traceId);
  const next: RecordingRow = {
    ...row,
    ...patch,
    trace_id: row.trace_id,
    envelope: row.envelope,
    updated_at: Date.now()
  };
  await db.recordings.put(next);
  return next;
}

async function requireRecording(traceId: string): Promise<RecordingRow> {
  const row = await db.recordings.get(traceId);
  if (!row) throw new Error(`recording not found: ${traceId}`);
  return row;
}

function uniqueNumbers(values: number[]): number[] {
  return [...new Set(values)].sort((a, b) => a - b);
}

function validateChunkUploadResponse(
  chunkIndex: number,
  sha256: string,
  response: { ok: boolean; chunkIndex: number; sha256: string }
): void {
  if (!response.ok || response.chunkIndex !== chunkIndex || response.sha256 !== sha256) {
    throw new Error(
      `chunk ${chunkIndex} response mismatch: expected ok=true index=${chunkIndex} sha256=${sha256}, got ok=${String(
        response.ok
      )} index=${response.chunkIndex} sha256=${response.sha256}`
    );
  }
}

function validateFinalizeResponse(traceId: string, response: { status: string; traceId: string }): void {
  if (response.traceId !== traceId || !response.status) {
    throw new Error(
      `finalize response mismatch: expected trace_id=${traceId} with a status, got trace_id=${response.traceId} status=${response.status}`
    );
  }
}
