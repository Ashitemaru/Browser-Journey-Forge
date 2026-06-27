import { afterEach, describe, expect, it } from 'vitest';
import { DEFAULT_CONFIG, JourneyForgeDB, db, getConfig, setConfig } from '@/storage/db';

describe('storage config', () => {
  let testDb: JourneyForgeDB | null = null;

  afterEach(async () => {
    await db.config.clear();
    if (testDb) {
      await testDb.delete();
      testDb.close();
      testDb = null;
    }
  });

  it('creates default config shape', async () => {
    testDb = new JourneyForgeDB('journey-forge-test-default');
    await testDb.config.put(DEFAULT_CONFIG);
    const row = await testDb.config.get('singleton');
    expect(row?.recording_mode).toBe('research_free_form');
    expect(row?.realUserConsentAccepted).toBe(false);
    expect(row?.capture.screenshots).toBe(false);
    expect(row?.capture.video).toBe(true);
  });

  it('indexes recordings by status', async () => {
    testDb = new JourneyForgeDB('journey-forge-test-recordings');
    await testDb.recordings.put({
      trace_id: 'tr_test',
      status: 'ready',
      created_at: 1,
      updated_at: 2,
      envelope: {
        schema_version: 'journey_trace_v1',
        trace_id: 'tr_test',
        recording_mode: 'research_free_form',
        started_at: '2026-06-03T00:00:00.000Z',
        tags: [],
        browser: { extension_version: '0.1.0', user_agent: 'test', timezone: 'UTC' },
        summary: {
          domains: [],
          duration_ms: 0,
          event_counts: {},
          screenshot_count: 0,
          video_chunk_count: 0
        }
      }
    });
    await expect(testDb.recordings.where('status').equals('ready').count()).resolves.toBe(1);
  });

  it('keeps config writes scoped to the singleton row', async () => {
    await db.config.clear();

    await expect(getConfig()).resolves.toEqual(DEFAULT_CONFIG);
    await setConfig({
      endpoint_url: 'https://api.example.test',
      realUserConsentAccepted: true,
      realUserConsentAcceptedAt: '2026-06-03T00:00:00.000Z',
      capture: { ...DEFAULT_CONFIG.capture, video: true }
    });

    const rows = await db.config.toArray();
    expect(rows).toHaveLength(1);
    expect(rows[0]).toEqual({
      ...DEFAULT_CONFIG,
      endpoint_url: 'https://api.example.test',
      recording_mode: 'research_free_form',
      realUserConsentAccepted: true,
      realUserConsentAcceptedAt: '2026-06-03T00:00:00.000Z',
      capture: {
        screenshots: false,
        video: true,
        networkBodies: true
      }
    });
  });
});
