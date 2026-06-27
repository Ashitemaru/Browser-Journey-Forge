import { describe, expect, it } from 'vitest';
import {
  createDomSnapshotDedupe,
  createDomSnapshotScheduler,
  shouldCaptureDomSnapshotAfterAction,
} from '@/capture/session-side-effects';
import type { ActionEvent, DomSnapshotEvent } from '@/shared/types';

describe('session side effects', () => {
  it('keeps wheel gestures from triggering full DOM snapshots', () => {
    expect(
      shouldCaptureDomSnapshotAfterAction(action('ev_wheel', 'wheel'))
    ).toBe(false);
    expect(
      shouldCaptureDomSnapshotAfterAction(action('ev_click', 'click'))
    ).toBe(true);
    expect(
      shouldCaptureDomSnapshotAfterAction(action('ev_input', 'input'))
    ).toBe(false);
    expect(
      shouldCaptureDomSnapshotAfterAction(action('ev_focus', 'focus'))
    ).toBe(false);
    expect(
      shouldCaptureDomSnapshotAfterAction(action('ev_scroll', 'scroll'))
    ).toBe(false);
  });

  it('rate-limits DOM snapshots for long recordings', () => {
    let current = 1_000;
    const scheduler = createDomSnapshotScheduler({
      minIntervalMs: 10_000,
      now: () => current,
    });

    expect(
      scheduler.shouldCaptureAfterAction(action('ev_click_1', 'click'))
    ).toBe(true);
    current += 2_000;
    expect(
      scheduler.shouldCaptureAfterAction(action('ev_click_2', 'click'))
    ).toBe(false);
    current += 8_000;
    expect(
      scheduler.shouldCaptureAfterAction(action('ev_submit', 'submit'))
    ).toBe(true);
    current += 20_000;
    expect(
      scheduler.shouldCaptureAfterAction(action('ev_input_late', 'input'))
    ).toBe(false);
  });

  it('drops repeated DOM snapshots with the same trace hash', () => {
    const dedupe = createDomSnapshotDedupe();

    expect(dedupe.shouldSend(snapshot('tr_one', 'hash_a'))).toBe(true);
    expect(dedupe.shouldSend(snapshot('tr_one', 'hash_a'))).toBe(false);
    expect(
      dedupe.shouldSend(
        snapshot('tr_one', 'hash_a', 'https://example.test/next')
      )
    ).toBe(true);
    expect(dedupe.shouldSend(snapshot('tr_one', 'hash_b'))).toBe(true);
    expect(dedupe.shouldSend(snapshot('tr_two', 'hash_a'))).toBe(true);

    dedupe.clear('tr_one');

    expect(dedupe.shouldSend(snapshot('tr_one', 'hash_b'))).toBe(true);
  });
});

function action(
  eventId: string,
  actionType: ActionEvent['action_type']
): ActionEvent {
  return {
    event_id: eventId,
    trace_id: 'tr_side_effects',
    tab_id: 1,
    timestamp: 10,
    url: 'https://example.test',
    kind: 'action',
    action_type: actionType,
  };
}

function snapshot(
  traceId: string,
  hash: string,
  url = 'https://example.test'
): DomSnapshotEvent {
  return {
    event_id: `ev_${hash}`,
    trace_id: traceId,
    tab_id: 1,
    timestamp: 10,
    url,
    kind: 'dom_snapshot',
    hash,
    nodes: [],
  };
}
