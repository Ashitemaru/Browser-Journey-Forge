import Dexie, { type Table } from 'dexie';
import type { BlobRow, CapturedEvent, CaptureSettings, RecordingMode, RecordingRow, UploadManifest } from '@/shared/types';
import { DEFAULT_API_KEY, DEFAULT_ENDPOINT_URL } from '@/shared/product';

export type LocalePreference = 'auto' | 'en' | 'zh-CN';

export type ConfigRow = {
  id: 'singleton';
  endpoint_url: string;
  api_key: string;
  locale: LocalePreference;
  recording_mode: RecordingMode;
  realUserConsentAccepted: boolean;
  realUserConsentAcceptedAt?: string;
  capture: CaptureSettings;
};

export const DEFAULT_CONFIG: ConfigRow = {
  id: 'singleton',
  endpoint_url: DEFAULT_ENDPOINT_URL,
  api_key: DEFAULT_API_KEY,
  locale: 'auto',
  recording_mode: 'research_free_form',
  realUserConsentAccepted: false,
  capture: {
    screenshots: false,
    video: true,
    networkBodies: true
  }
};

export class JourneyForgeDB extends Dexie {
  recordings!: Table<RecordingRow, string>;
  events!: Table<CapturedEvent, string>;
  blobs!: Table<BlobRow, string>;
  uploadManifests!: Table<UploadManifest, string>;
  config!: Table<ConfigRow, 'singleton'>;

  constructor(name = 'journey-forge-local') {
    super(name);
    this.version(1).stores({
      recordings: 'trace_id, status, created_at, updated_at, upload_id',
      events: 'event_id, trace_id, kind, timestamp, [trace_id+timestamp]',
      blobs: 'blob_key, trace_id, kind, created_at',
      uploadManifests: 'trace_id, upload_id, finalized',
      config: 'id'
    });
  }
}

export const db = new JourneyForgeDB();

export async function getConfig(): Promise<ConfigRow> {
  const current = await db.config.get('singleton');
  if (!current) {
    await db.config.put(DEFAULT_CONFIG);
    return DEFAULT_CONFIG;
  }
  const merged = {
    ...DEFAULT_CONFIG,
    ...current,
    capture: { ...DEFAULT_CONFIG.capture, ...current.capture }
  };
  if (JSON.stringify(merged) !== JSON.stringify(current)) {
    await db.config.put(merged);
  }
  return merged;
}

export async function setConfig(patch: Partial<ConfigRow>): Promise<void> {
  const current = await getConfig();
  await db.config.put({
    ...current,
    ...patch,
    capture: { ...current.capture, ...(patch.capture ?? {}) },
    id: 'singleton'
  });
}
