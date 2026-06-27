import { browser } from 'wxt/browser';
import { downloadEventFromDelta, downloadEventFromItem } from '@/capture/download-events';
import { shouldRecordBrowserNavigationUrl, tabNavigationEvent } from '@/capture/tab-navigation';
import {
  appendEvent,
  setRecordingLabel,
  startRecording,
  stopRecording,
} from '@/recording/recorder';
import { errorMessage } from '@/shared/errors';
import type { CapturedEvent, CaptureSettings, RecordingRow } from '@/shared/types';
import { db } from '@/storage/db';
import { uploadRecording } from '@/upload/runner';

type RuntimeMessage =
  | { type: 'get-active-recording' }
  | { type: 'start-recording'; label?: string }
  | { type: 'stop-recording'; traceId?: string }
  | { type: 'event'; event: CapturedEvent }
  | { type: 'resume-upload'; traceId: string; label?: string }
  | { type: 'delete-recording'; traceId: string };

type SenderLike = {
  tab?: {
    id?: number;
    windowId?: number;
    url?: string;
    active?: boolean;
  };
};

let activeTraceId: string | null = null;
let activeTraceRecovered = false;
let recovery: Promise<void> | null = null;
const CONTENT_FLUSH_TIMEOUT_MS = 1500;
const tabUrlCache = new Map<number, string>();
const DEFAULT_CAPTURE_SETTINGS: CaptureSettings = {
  screenshots: false,
  video: false,
  networkBodies: true
};

export default defineBackground(() => {
  recovery = recoverActiveTraceId();
  void refreshRecordingBadge();

  const listener = (message: unknown, sender: SenderLike) => handleMessage(message, sender);
  browser.runtime.onMessage.addListener(listener as Parameters<typeof browser.runtime.onMessage.addListener>[0]);
  browser.tabs.onCreated.addListener((tab) => {
    void handleTabCreated(tab);
  });
  browser.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    void handleTabUpdated(tabId, changeInfo, tab);
  });
  browser.tabs.onRemoved.addListener((tabId) => {
    void handleTabRemoved(tabId);
  });
  browser.webNavigation.onCommitted.addListener((details) => {
    void handleNavigationCommitted(details);
  });

  const downloads = browser.downloads as unknown as typeof chrome.downloads | undefined;
  downloads?.onCreated?.addListener((item) => {
    void appendDownloadCreated(item);
  });
  downloads?.onChanged?.addListener((delta) => {
    void appendDownloadChanged(delta);
  });
});

async function handleMessage(message: unknown, sender: SenderLike): Promise<unknown> {
  if (!isRuntimeMessage(message)) return undefined;
  await ensureRecovered();

  switch (message.type) {
    case 'get-active-recording': {
      const activeRow = activeTraceId ? ((await db.recordings.get(activeTraceId)) ?? null) : null;
      return {
        active: activeTraceId !== null,
        traceId: activeTraceId,
        recovered: activeTraceRecovered,
        captureSettings: await captureSettingsForActiveRecording(activeTraceId, activeRow),
        row: activeRow
      };
    }

    case 'start-recording': {
      const row = await beginRecording(message.label);
      const captureSettings = await captureSettingsForActiveRecording(row.trace_id, row);
      return { active: true, traceId: activeTraceId, recovered: false, captureSettings, row };
    }

    case 'stop-recording': {
      const traceId = message.traceId ?? activeTraceId;
      if (!traceId) return { active: false, traceId: null };
      await flushRecordingTabs(traceId);
      const row = await stopRecording(traceId);
      if (activeTraceId === traceId) activeTraceId = null;
      activeTraceRecovered = false;
      await broadcastRecordingState(false, null, null);
      await refreshRecordingBadge();
      return { active: false, traceId: null, recovered: false, captureSettings: null, row };
    }

    case 'event': {
      if (!activeTraceId && !message.event.trace_id) return { ok: false };
      const event = normalizeEvent(message.event, sender, activeTraceId ?? message.event.trace_id);
      await appendEvent(event);
      return { ok: true };
    }

    case 'resume-upload': {
      await ensureUploadable(message.traceId, message.label);
      const row = await uploadRecording(message.traceId);
      return { ok: true, row };
    }

    case 'delete-recording': {
      await deleteRecordingLocal(message.traceId);
      if (activeTraceId === message.traceId) {
        activeTraceId = null;
        activeTraceRecovered = false;
        await broadcastRecordingState(false, null, null);
        await refreshRecordingBadge();
      }
      return { ok: true };
    }
  }
}

async function beginRecording(label?: string): Promise<RecordingRow> {
  const row = await startRecording(undefined, label ? { label } : undefined);
  activeTraceId = row.trace_id;
  activeTraceRecovered = false;
  const captureSettings = await captureSettingsForActiveRecording(activeTraceId, row);
  await broadcastRecordingState(true, activeTraceId, captureSettings);
  await refreshRecordingBadge();
  return row;
}

// A just-stopped recording is `ready`. If the user typed a task name at upload
// time, persist it on the row before the upload runner reads the envelope.
async function ensureUploadable(traceId: string, label?: string): Promise<void> {
  const trimmed = label?.trim();
  if (!trimmed) return;
  const row = await db.recordings.get(traceId);
  if (!row || row.status !== 'ready') return;
  await setRecordingLabel(traceId, trimmed);
}

async function recoverActiveTraceId(): Promise<void> {
  const active = await db.recordings.where('status').equals('recording').toArray();
  active.sort((left, right) => right.updated_at - left.updated_at);
  const recoveredRow = active[0] ?? null;
  activeTraceId = recoveredRow?.trace_id ?? null;
  activeTraceRecovered = activeTraceId !== null;
}

async function ensureRecovered(): Promise<void> {
  if (recovery) {
    await recovery;
    recovery = null;
  }
}

function normalizeEvent(event: CapturedEvent, sender: SenderLike, traceId: string): CapturedEvent {
  return {
    ...event,
    trace_id: traceId,
    tab_id: sender.tab?.id ?? event.tab_id ?? -1,
    url: event.url || sender.tab?.url || ''
  } as CapturedEvent;
}

async function refreshRecordingBadge(): Promise<void> {
  const action = chrome.action;
  if (!action?.setBadgeText) return;
  try {
    if (activeTraceId) {
      await action.setBadgeBackgroundColor({ color: '#e0584b' });
      await action.setBadgeText({ text: '●' });
    } else {
      await action.setBadgeText({ text: '' });
    }
  } catch {
    // Badge updates are best-effort and must never break recording.
  }
}

async function handleTabCreated(tab: { id?: number; url?: string; pendingUrl?: string; openerTabId?: number }): Promise<void> {
  if (tab.id === undefined) return;
  const url = tab.url ?? tab.pendingUrl ?? 'about:blank';
  if (shouldRecordBrowserNavigationUrl(url)) tabUrlCache.set(tab.id, url);
  await ensureRecovered();
  if (!activeTraceId || !shouldRecordBrowserNavigationUrl(url)) return;
  await appendNavigationEvent(
    tabNavigationEvent({
      traceId: activeTraceId,
      tabId: tab.id,
      timestamp: Date.now(),
      url,
      navType: 'tabOpened',
      ...(tab.openerTabId !== undefined ? { openerTabId: tab.openerTabId } : {})
    })
  );
}

async function handleTabUpdated(
  tabId: number,
  changeInfo: { url?: string },
  tab: { url?: string; pendingUrl?: string }
): Promise<void> {
  const url = changeInfo.url ?? tab.url ?? tab.pendingUrl;
  if (shouldRecordBrowserNavigationUrl(url)) tabUrlCache.set(tabId, url);
}

async function handleTabRemoved(tabId: number): Promise<void> {
  const url = tabUrlCache.get(tabId) ?? 'about:blank';
  tabUrlCache.delete(tabId);
  await ensureRecovered();
  if (!activeTraceId || !shouldRecordBrowserNavigationUrl(url)) return;
  await appendNavigationEvent(
    tabNavigationEvent({
      traceId: activeTraceId,
      tabId,
      timestamp: Date.now(),
      url,
      navType: 'tabClosed'
    })
  );
}

async function handleNavigationCommitted(details: { tabId: number; frameId: number; url: string }): Promise<void> {
  if (details.frameId !== 0 || !shouldRecordBrowserNavigationUrl(details.url)) return;
  const fromUrl = tabUrlCache.get(details.tabId);
  tabUrlCache.set(details.tabId, details.url);
  await ensureRecovered();
  if (!activeTraceId) return;
  await appendNavigationEvent(
    tabNavigationEvent({
      traceId: activeTraceId,
      tabId: details.tabId,
      timestamp: Date.now(),
      url: details.url,
      navType: 'load',
      ...(fromUrl && fromUrl !== details.url ? { fromUrl } : {})
    })
  );
}

function warnDroppedEvent(kind: string): (error: unknown) => void {
  return (error) =>
    console.warn(`[journey-forge] dropped ${kind} event: ${errorMessage(error)}`);
}

async function appendDownloadCreated(item: chrome.downloads.DownloadItem): Promise<void> {
  await ensureRecovered();
  if (!activeTraceId) return;
  await appendEvent(downloadEventFromItem({ traceId: activeTraceId, timestamp: Date.now(), item }) as unknown as CapturedEvent).catch(warnDroppedEvent('download'));
}

async function appendDownloadChanged(delta: chrome.downloads.DownloadDelta): Promise<void> {
  await ensureRecovered();
  if (!activeTraceId) return;
  const event = downloadEventFromDelta({ traceId: activeTraceId, timestamp: Date.now(), delta });
  if (event) await appendEvent(event as unknown as CapturedEvent).catch(warnDroppedEvent('download'));
}

async function appendNavigationEvent(event: CapturedEvent): Promise<void> {
  await appendEvent(event).catch(warnDroppedEvent('navigation'));
}

async function captureSettingsForActiveRecording(traceId: string | null, row: RecordingRow | null): Promise<CaptureSettings | null> {
  if (row?.envelope.capture_settings) return row.envelope.capture_settings;
  if (traceId) return DEFAULT_CAPTURE_SETTINGS;
  return null;
}

async function broadcastRecordingState(
  active: boolean,
  traceId: string | null,
  captureSettings?: CaptureSettings | null
): Promise<void> {
  const resolvedCaptureSettings = captureSettings === undefined ? await captureSettingsForActiveRecording(traceId, null) : captureSettings;
  const tabs = await browser.tabs.query({ url: ['http://*/*', 'https://*/*'] });
  await Promise.allSettled(
    tabs
      .filter((tab) => tab.id !== undefined)
      .map((tab) =>
        browser.tabs.sendMessage(tab.id!, {
          type: 'recording-state',
          active,
          traceId,
          captureSettings: resolvedCaptureSettings
        })
      )
  );
}

async function flushRecordingTabs(traceId: string): Promise<void> {
  const tabs = await browser.tabs.query({ url: ['http://*/*', 'https://*/*'] });
  await Promise.allSettled(
    tabs
      .filter((tab) => tab.id !== undefined)
      .map((tab) =>
        withTimeout(
          browser.tabs.sendMessage(tab.id!, {
            type: 'flush-recording-events',
            traceId,
            timeoutMs: CONTENT_FLUSH_TIMEOUT_MS
          }),
          CONTENT_FLUSH_TIMEOUT_MS + 250
        )
      )
  );
}

async function deleteRecordingLocal(traceId: string): Promise<void> {
  await db.transaction('rw', db.recordings, db.events, db.blobs, db.uploadManifests, async () => {
    await Promise.all([
      db.recordings.delete(traceId),
      db.events.where('trace_id').equals(traceId).delete(),
      db.blobs.where('trace_id').equals(traceId).delete(),
      db.uploadManifests.delete(traceId)
    ]);
  });
}

async function withTimeout<T>(promise: Promise<T>, timeoutMs: number): Promise<T | null> {
  return await Promise.race([
    promise.catch(() => null),
    new Promise<null>((resolve) => setTimeout(() => resolve(null), timeoutMs))
  ]);
}

function isRuntimeMessage(message: unknown): message is RuntimeMessage {
  if (!message || typeof message !== 'object') return false;
  const type = (message as { type?: unknown }).type;
  return (
    type === 'get-active-recording' ||
    type === 'start-recording' ||
    type === 'stop-recording' ||
    type === 'event' ||
    (type === 'resume-upload' && typeof (message as { traceId?: unknown }).traceId === 'string') ||
    (type === 'delete-recording' && typeof (message as { traceId?: unknown }).traceId === 'string')
  );
}
