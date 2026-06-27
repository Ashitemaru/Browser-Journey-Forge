import { beforeEach, describe, expect, it, vi } from 'vitest';
import { getBrowserAdapter } from '@/browser';

const browserApi = vi.hoisted(() => ({
  alarms: {
    create: vi.fn()
  },
  runtime: {
    getURL: vi.fn()
  },
  tabs: {
    create: vi.fn()
  }
}));

vi.mock('wxt/browser', () => ({
  browser: browserApi
}));

describe('browser adapter selection', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    browserApi.alarms.create.mockReset();
    browserApi.runtime.getURL.mockReset();
    browserApi.tabs.create.mockReset();
  });

  it('always returns the chrome adapter capabilities', () => {
    expect(getBrowserAdapter().capabilities).toMatchObject({
      browser: 'chrome',
      screenshots: false,
      video: false
    });
  });
});
