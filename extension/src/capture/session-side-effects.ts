import type { ActionEvent, DomSnapshotEvent } from '@/shared/types';

const DEFAULT_DOM_SNAPSHOT_INTERVAL_MS = 10_000;

const DOM_SNAPSHOT_ACTION_TYPES = new Set<ActionEvent['action_type']>([
  'click',
  'dblclick',
  'change',
  'submit',
  'keydown',
  'file_select',
]);

export function shouldCaptureDomSnapshotAfterAction(
  event: ActionEvent
): boolean {
  return DOM_SNAPSHOT_ACTION_TYPES.has(event.action_type);
}

export function createDomSnapshotScheduler(
  options: { minIntervalMs?: number; now?: () => number } = {}
) {
  const now = options.now ?? (() => Date.now());
  const minIntervalMs =
    options.minIntervalMs ?? DEFAULT_DOM_SNAPSHOT_INTERVAL_MS;
  let lastCaptureAt = Number.NEGATIVE_INFINITY;

  return {
    shouldCaptureAfterAction(event: ActionEvent): boolean {
      if (!shouldCaptureDomSnapshotAfterAction(event)) return false;
      const current = now();
      if (current - lastCaptureAt < minIntervalMs) return false;
      lastCaptureAt = current;
      return true;
    },

    reset(): void {
      lastCaptureAt = Number.NEGATIVE_INFINITY;
    },
  };
}

export function createDomSnapshotDedupe() {
  const lastKeyByTrace = new Map<string, string>();

  return {
    shouldSend(snapshot: DomSnapshotEvent): boolean {
      const key = `${snapshot.url}\n${snapshot.hash}`;
      const lastKey = lastKeyByTrace.get(snapshot.trace_id);
      if (lastKey === key) return false;
      lastKeyByTrace.set(snapshot.trace_id, key);
      return true;
    },

    clear(traceId: string): void {
      lastKeyByTrace.delete(traceId);
    },
  };
}
