/**
 * Update awareness, front end (policy lives in src/mai2srt/update.py).
 *
 * A store rather than component state because two places need the same answer:
 * the sidebar version button (is there something newer?) and the About card it
 * opens. Keeping it out of the page also means a check that finishes while the
 * card is closed is already there when it opens.
 *
 * CADENCE IS NOT DECIDED HERE. The backend caches one answer for 24 h, so
 * `refresh(false)` can be called on boot, on a timer, and when the card opens
 * without producing extra requests -- one rule, one place. `force` is the
 * card's "check now" button and is the only thing that bypasses it.
 */
import { useSyncExternalStore } from 'react';
import {
  checkUpdate, setUpdateAuto, type UpdateInfo,
} from './api';

export interface UpdateState {
  /** the running version, as the backend reports it */
  current: string;
  auto: boolean;
  checking: boolean;
  latest: UpdateInfo | null;
  /** last failure. Shown inside the card; never toasted -- an automatic check
   *  that cannot reach GitHub is not the user's problem, and a box that
   *  complains about the network on every launch would be. */
  error: string;
  checkedAt: string | null;
  /** at least one answer (or failure) has come back this launch */
  loaded: boolean;
}

let state: UpdateState = {
  current: '',
  auto: true,
  checking: false,
  latest: null,
  error: '',
  checkedAt: null,
  loaded: false,
};

const listeners = new Set<() => void>();

function set(partial: Partial<UpdateState>): void {
  state = { ...state, ...partial };
  listeners.forEach((l) => l());
}

function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

/** Primitive selectors only at call sites: the sidebar reads `latest?.version`
 *  and nothing else, so it must not re-render when the card's error text
 *  changes. */
export function useUpdate<T>(select: (s: UpdateState) => T): T {
  return useSyncExternalStore(subscribe, () => select(state));
}

let inFlight: Promise<void> | null = null;

/** Ask for the current answer. Collapses concurrent calls (StrictMode mounts
 *  the shell twice in dev) and never throws: a network failure is data too. */
export function refreshUpdate(force = false): Promise<void> {
  if (inFlight) return inFlight;
  set({ checking: true });
  inFlight = checkUpdate(force)
    .then((st) => set({
      current: st.current,
      auto: st.auto,
      latest: st.latest,
      error: st.error ?? '',
      checkedAt: st.checked_at,
      loaded: true,
    }))
    .catch((e) => set({ error: String(e).slice(0, 200), loaded: true }))
    .finally(() => {
      inFlight = null;
      set({ checking: false });
    });
  return inFlight;
}

/** Turn the automatic check on or off. Optimistic: a toggle that waits for a
 *  roundtrip feels broken, and the write is one boolean. */
export async function setAutomaticChecks(on: boolean): Promise<void> {
  set({ auto: on });
  try {
    const s = await setUpdateAuto(on);
    set({ auto: s.auto });
    // switching it back on should produce an answer instead of an empty card
    if (on) void refreshUpdate(false);
  } catch {
    set({ auto: !on });
  }
}

let booted = false;

/** Startup: one check, then a slow poll. Both are cheap because the backend
 *  answers from its own cache -- the poll exists so a window left open for
 *  days still learns about a release, not to increase the request rate. */
export function updateBoot(): void {
  if (booted) return;
  booted = true;
  void refreshUpdate(false);
  // 6 h against a 24 h TTL: at most one real request per day, and the answer
  // cannot be more than six hours stale whenever the app stays open
  window.setInterval(() => { void refreshUpdate(false); }, 6 * 3600 * 1000);
}
