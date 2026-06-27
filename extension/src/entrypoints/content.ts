import {
  installActionRecorder,
  type InstalledCapture,
} from '@/capture/action-recorder';
import { captureDomSnapshot } from '@/capture/dom-snapshot';
import { installFormSummaryRecorder } from '@/capture/form-summary-recorder';
import { installMutationSummaryRecorder } from '@/capture/mutation-summary-recorder';
import { installNetworkBridge } from '@/capture/network-bridge';
import {
  networkHookConfig,
  type NetworkHookConfig,
} from '@/capture/network-events';
import {
  navigationHookConfig,
  type NavigationHookConfig,
} from '@/capture/navigation-events';
import {
  installNavigationRecorder,
  shouldInstallPageNavigationCapture,
} from '@/capture/navigation-recorder';
import {
  createDomSnapshotDedupe,
  createDomSnapshotScheduler,
} from '@/capture/session-side-effects';
import type { CapturedEvent, CaptureSettings } from '@/shared/types';

export default defineContentScript({
  matches: ['<all_urls>'],
  allFrames: true,
  main() {
    if (!isCaptureUrl(location.href)) return;

    void refreshRecordingState();
    chrome.runtime.onMessage.addListener(handleRuntimeMessage);
  },
});

type ActiveRecordingResponse = {
  active: boolean;
  traceId: string | null;
  captureSettings?: CaptureSettings | null;
  capturePaused?: boolean;
};

type RecordingStateMessage = {
  type: 'recording-state';
  active: boolean;
  traceId: string | null;
  captureSettings?: CaptureSettings | null;
  capturePaused?: boolean;
};

type FlushRecordingEventsMessage = {
  type: 'flush-recording-events';
  traceId?: string;
  timeoutMs?: number;
};

let actionRecorder: InstalledCapture | null = null;
let formSummaryRecorder: InstalledCapture | null = null;
let mutationSummaryRecorder: InstalledCapture | null = null;
let networkBridge: InstalledCapture | null = null;
let navigationRecorder: InstalledCapture | null = null;
let currentTraceId: string | null = null;
let currentCapturePaused = false;
let currentNetworkConfig: NetworkHookConfig | null = null;
let currentNavigationConfig: NavigationHookConfig | null = null;
let currentCaptureBodies: boolean | null = null;
const pendingSends = new Set<Promise<unknown>>();
let recordingStateQueue: Promise<void> = Promise.resolve();
const domSnapshotDedupe = createDomSnapshotDedupe();
const domSnapshotScheduler = createDomSnapshotScheduler();

async function refreshRecordingState(): Promise<void> {
  const response = (await chrome.runtime.sendMessage({
    type: 'get-active-recording',
  })) as ActiveRecordingResponse;
  await enqueueRecordingState(
    response.active,
    response.traceId,
    response.captureSettings,
    Boolean(response.capturePaused)
  );
}

function enqueueRecordingState(
  active: boolean,
  traceId: string | null,
  captureSettings?: CaptureSettings | null,
  capturePaused = false
): Promise<void> {
  const next = recordingStateQueue.then(() =>
    applyRecordingState(active, traceId, captureSettings, capturePaused)
  );
  recordingStateQueue = next.catch(() => undefined);
  return next;
}

async function applyRecordingState(
  active: boolean,
  traceId: string | null,
  captureSettings?: CaptureSettings | null,
  capturePaused = false
): Promise<void> {
  if (!active || !traceId) {
    stopCapture();
    return;
  }
  if (capturePaused) {
    stopCapture({ keepTraceId: traceId, capturePaused: true });
    return;
  }
  const captureBodies = captureSettings?.networkBodies !== false;
  const shouldInstallNavigationRecorder = shouldInstallPageNavigationCapture(
    isTopFrame()
  );
  const captureAlreadyInstalled =
    actionRecorder &&
    formSummaryRecorder &&
    mutationSummaryRecorder &&
    networkBridge &&
    (!shouldInstallNavigationRecorder || navigationRecorder) &&
    currentCaptureBodies === captureBodies &&
    currentCapturePaused === false;
  if (currentTraceId === traceId && captureAlreadyInstalled) return;

  stopCapture();
  currentTraceId = traceId;
  currentCapturePaused = false;
  currentNetworkConfig = networkHookConfig(createToken(), captureBodies);
  currentNavigationConfig = shouldInstallNavigationRecorder
    ? navigationHookConfig(createToken())
    : null;
  currentCaptureBodies = captureBodies;
  await injectPageHook(currentNetworkConfig, currentNavigationConfig);

  const sendEvent = (event: CapturedEvent) => {
    const send = sendToBackground({ type: 'event', event }).then(
      () => undefined
    );
    if (event.kind === 'action') {
      if (domSnapshotScheduler.shouldCaptureAfterAction(event)) {
        trackPending(captureAndSendSnapshot(traceId, event.event_id));
      }
    }
    return send;
  };

  actionRecorder = installActionRecorder({ traceId, sendEvent });
  formSummaryRecorder = installFormSummaryRecorder({ traceId, sendEvent });
  mutationSummaryRecorder = installMutationSummaryRecorder({
    traceId,
    sendEvent,
  });
  networkBridge = installNetworkBridge({
    traceId,
    ...currentNetworkConfig,
    captureBodies,
    sendEvent,
  });
  navigationRecorder = currentNavigationConfig
    ? installNavigationRecorder({
        traceId,
        ...currentNavigationConfig,
        sendEvent,
      })
    : null;
  trackPending(captureAndSendSnapshot(traceId));
}

async function captureAndSendSnapshot(
  traceId: string,
  triggerEventId?: string
): Promise<void> {
  const event = await captureDomSnapshot({
    traceId,
    ...(triggerEventId ? { triggerEventId } : {}),
  });
  if (!domSnapshotDedupe.shouldSend(event)) return;
  await sendToBackground({ type: 'event', event });
}

function stopCapture(
  options: { keepTraceId?: string | null; capturePaused?: boolean } = {}
): void {
  const stoppedTraceId = currentTraceId;
  deactivatePageHook();
  actionRecorder?.stop();
  formSummaryRecorder?.stop();
  mutationSummaryRecorder?.stop();
  networkBridge?.stop();
  navigationRecorder?.stop();
  actionRecorder = null;
  formSummaryRecorder = null;
  mutationSummaryRecorder = null;
  networkBridge = null;
  navigationRecorder = null;
  currentTraceId = options.keepTraceId ?? null;
  currentCapturePaused = Boolean(options.capturePaused);
  currentNetworkConfig = null;
  currentNavigationConfig = null;
  currentCaptureBodies = null;
  if (stoppedTraceId && !options.keepTraceId) {
    domSnapshotDedupe.clear(stoppedTraceId);
    domSnapshotScheduler.reset();
  }
}

async function injectPageHook(
  networkConfig: NetworkHookConfig,
  navigationConfig: NavigationHookConfig | null
): Promise<void> {
  const script = document.createElement('script');
  // The hash is visible to page JS via DOM observation. It only hardens against
  // blind forged events; bridges still treat page-context detail as untrusted.
  script.src = `${chrome.runtime.getURL('injected.js')}#${encodeURIComponent(
    JSON.stringify({
      network: {
        channel: networkConfig.channel,
        captureBodies: networkConfig.captureBodies,
      },
      ...(navigationConfig
        ? { navigation: { channel: navigationConfig.channel } }
        : {}),
    })
  )}`;
  script.async = false;
  await new Promise<void>((resolve) => {
    script.onload = () => {
      script.remove();
      resolve();
    };
    script.onerror = () => resolve();
    (document.head || document.documentElement).append(script);
  });
}

function deactivatePageHook(): void {
  if (currentNetworkConfig) {
    window.dispatchEvent(
      new CustomEvent(currentNetworkConfig.deactivateEventName)
    );
  }
  if (currentNavigationConfig) {
    window.dispatchEvent(
      new CustomEvent(currentNavigationConfig.deactivateEventName)
    );
  }
}

function sendToBackground(message: unknown): Promise<unknown> {
  return trackPending(
    chrome.runtime.sendMessage(message).catch(() => undefined)
  );
}

function trackPending<T>(promise: Promise<T>): Promise<T> {
  const tracked = promise.finally(() => pendingSends.delete(tracked));
  pendingSends.add(tracked);
  return tracked;
}

async function flushPendingEvents(
  timeoutMs = 1500
): Promise<{ drained: boolean; pending: number }> {
  const pending = [...pendingSends];
  if (!pending.length) return { drained: true, pending: 0 };
  const result = await Promise.race([
    Promise.allSettled(pending).then(() => true),
    new Promise<false>((resolve) =>
      setTimeout(() => resolve(false), timeoutMs)
    ),
  ]);
  return { drained: result === true, pending: pendingSends.size };
}

function handleRuntimeMessage(
  message: unknown,
  _sender: chrome.runtime.MessageSender,
  sendResponse: (response?: unknown) => void
) {
  if (
    isFlushRecordingEventsMessage(message) &&
    (!message.traceId || message.traceId === currentTraceId)
  ) {
    void flushPendingEvents(message.timeoutMs).then(sendResponse);
    return true;
  }
  if (isRecordingStateMessage(message)) {
    void enqueueRecordingState(
      message.active,
      message.traceId,
      message.captureSettings,
      Boolean(message.capturePaused)
    );
  }
  return undefined;
}

function createToken(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join(
    ''
  );
}

function isTopFrame(): boolean {
  return window.top === window;
}

function isCaptureUrl(url: string): boolean {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:';
  } catch {
    return false;
  }
}

function isRecordingStateMessage(
  message: unknown
): message is RecordingStateMessage {
  if (!message || typeof message !== 'object') return false;
  const value = message as Partial<RecordingStateMessage>;
  return value.type === 'recording-state' && typeof value.active === 'boolean';
}

function isFlushRecordingEventsMessage(
  message: unknown
): message is FlushRecordingEventsMessage {
  if (!message || typeof message !== 'object') return false;
  const value = message as Partial<FlushRecordingEventsMessage>;
  return value.type === 'flush-recording-events';
}
