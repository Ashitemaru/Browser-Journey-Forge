import { describe, expect, it } from 'vitest';
import {
  createTranslator,
  dictionaries,
  effectiveLocale,
  normalizeLocale,
  translate,
} from '@/i18n';
import {
  buildUploadQueueStats,
  uploadQueueChunkDetail,
  uploadQueueChunkLabel,
} from '@/upload/queue';
import type { RecordingRow } from '@/shared/types';

describe('i18n', () => {
  it('keeps English and Chinese dictionaries aligned', () => {
    expect(Object.keys(dictionaries['zh-CN']).sort()).toEqual(
      Object.keys(dictionaries.en).sort()
    );
  });

  it('normalizes browser locales and honors explicit preference', () => {
    expect(normalizeLocale('zh-Hans-CN')).toBe('zh-CN');
    expect(normalizeLocale('en-US')).toBe('en');
    expect(
      effectiveLocale('auto', {
        language: 'en-US',
        languages: ['zh-CN', 'en-US'],
      } as Pick<Navigator, 'language' | 'languages'>)
    ).toBe('zh-CN');
    expect(
      effectiveLocale('en', {
        language: 'zh-CN',
        languages: ['zh-CN'],
      } as Pick<Navigator, 'language' | 'languages'>)
    ).toBe('en');
  });

  it('interpolates translated operational copy', () => {
    expect(translate('en', 'queue.chunkUploaded', { uploaded: 2, total: 4 })).toBe(
      '2/4 chunks uploaded'
    );
    expect(
      translate('zh-CN', 'queue.chunkUploaded', { uploaded: 2, total: 4 })
    ).toBe('已上传 2/4 个分块');
  });

  it('localizes queue labels', () => {
    const zh = createTranslator('zh-CN');
    const stats = buildUploadQueueStats(recording(), undefined);
    expect(uploadQueueChunkLabel(stats, zh)).toBe('暂无清单');
    expect(uploadQueueChunkDetail(stats, zh)).toBe('上传开始后将生成清单。');
  });
});

function recording(): RecordingRow {
  return {
    trace_id: 'tr_i18n',
    status: 'ready',
    created_at: 1,
    updated_at: 2,
    envelope: {
      schema_version: 'journey_trace_v1',
      trace_id: 'tr_i18n',
      recording_mode: 'research_free_form',
      started_at: '2026-06-05T00:00:00.000Z',
      tags: [],
      browser: {
        extension_version: '0.1.0',
        user_agent: 'vitest',
        timezone: 'UTC',
      },
      summary: {
        domains: [],
        duration_ms: 0,
        event_counts: {},
        screenshot_count: 0,
        video_chunk_count: 0,
      },
    },
  };
}
