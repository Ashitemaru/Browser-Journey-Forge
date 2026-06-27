import type { BrowserAdapter } from './adapter';
import { chromeAdapter } from './chrome-adapter';

// The extension ships chrome-only, so the chrome adapter is always used.
export function getBrowserAdapter(): BrowserAdapter {
  return chromeAdapter;
}
