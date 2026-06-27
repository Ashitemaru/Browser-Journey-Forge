import type { RecordingRow, UploadManifest } from '@/shared/types';

export type UploadQueueStats = {
  totalChunks: number;
  uploadedChunks: number;
  pendingChunks: number;
  retryChunks: number;
  mediaChunks: number;
  pendingMediaChunks: number;
  eventChunks: number;
  totalBytes: number;
  uploadedBytes: number;
  pendingBytes: number;
  finalized: boolean;
  hasManifest: boolean;
  lastError: string | null;
};

const RETRY_STATUSES = new Set<RecordingRow['status']>(['failed']);

export function buildUploadQueueStats(
  recording: RecordingRow,
  manifest: UploadManifest | undefined
): UploadQueueStats {
  const chunks = manifest?.chunks ?? [];
  const pendingChunks = chunks.filter((chunk) => !chunk.uploaded);
  const uploadedChunks = chunks.filter((chunk) => chunk.uploaded);
  return {
    totalChunks: chunks.length,
    uploadedChunks: uploadedChunks.length,
    pendingChunks: pendingChunks.length,
    retryChunks: RETRY_STATUSES.has(recording.status) ? pendingChunks.length : 0,
    mediaChunks: chunks.filter((chunk) => chunk.kind === 'media').length,
    pendingMediaChunks: pendingChunks.filter((chunk) => chunk.kind === 'media').length,
    eventChunks: chunks.filter((chunk) => chunk.kind === 'events').length,
    totalBytes: chunks.reduce((sum, chunk) => sum + chunk.bytes, 0),
    uploadedBytes: uploadedChunks.reduce((sum, chunk) => sum + chunk.bytes, 0),
    pendingBytes: pendingChunks.reduce((sum, chunk) => sum + chunk.bytes, 0),
    finalized: Boolean(manifest?.finalized),
    hasManifest: Boolean(manifest),
    lastError: recording.last_error ?? null,
  };
}

export function uploadQueueChunkLabel(stats: UploadQueueStats): string {
  if (!stats.hasManifest) return 'No manifest yet';
  return `${stats.uploadedChunks}/${stats.totalChunks} chunks uploaded`;
}

export function uploadQueueChunkDetail(stats: UploadQueueStats): string {
  if (!stats.hasManifest) return 'Manifest will be built when upload starts.';
  const parts = [
    `${formatBytes(stats.uploadedBytes)} of ${formatBytes(stats.totalBytes)}`,
    `${stats.pendingChunks} pending`,
    `${stats.pendingMediaChunks}/${stats.mediaChunks} media pending`,
  ];
  if (stats.retryChunks > 0) {
    parts.push(`${stats.retryChunks} retry chunks`);
  }
  if (stats.finalized) parts.push('finalized');
  return parts.join(' - ');
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KiB', 'MiB', 'GiB'];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${Math.round(value * 10) / 10} ${units[unitIndex]}`;
}
