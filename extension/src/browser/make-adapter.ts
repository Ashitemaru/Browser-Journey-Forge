import { browser } from 'wxt/browser';
import type { BrowserAdapter } from './adapter';
import type { BrowserCapabilities } from '@/shared/types';

/**
 * Capabilities shared by the chrome adapter. The adapter spreads this and sets
 * `browser` (and overrides anything that differs).
 */
export const BASE_CAPABILITIES = {
  screenshots: false,
  video: false,
  webRequestBody: true,
  injectedResponseBody: true,
} satisfies Omit<BrowserCapabilities, 'browser'>;

/**
 * Builds a browser adapter on top of WXT's `browser` namespace (polyfilled on
 * Chrome).
 */
export function makeBrowserAdapter(capabilities: BrowserCapabilities): BrowserAdapter {
  return {
    capabilities,
    createAlarm(name, periodInMinutes) {
      void browser.alarms.create(name, { periodInMinutes });
    },
  };
}
