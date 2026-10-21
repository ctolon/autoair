import { useRef } from "react";

/**
 * Wraps a store selector so it keeps returning its previous result while `equal(previous,
 * next)` holds (like zustand's `useShallow`, with a custom comparison). The component then
 * skips re-renders for changes it does not care about, e.g. the inspector while its node is
 * dragged (the node object changes on every frame, its spec does not).
 */
export function useStableSelector<S, T>(selector: (state: S) => T, equal: (a: T, b: T) => boolean): (state: S) => T {
  const prev = useRef<{ value: T } | null>(null);
  return (state: S) => {
    const next = selector(state);
    if (prev.current && equal(prev.current.value, next)) return prev.current.value;
    prev.current = { value: next };
    return next;
  };
}
