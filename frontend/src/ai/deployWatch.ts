import { useBuilder } from "../store/builder";

type DeployedInfo = { dagId: string; etag: string | null; prUrl: string | null };

let cleanup: (() => void) | null = null;

/**
 * Calls `onDeployed` once the DAG `dagId` is deployed through the normal deploy pipeline
 * (the builder marks it saved with a new version or pull request). An AI proposal applied
 * to the editor uses this to record that it went live. A newer watch replaces this one;
 * loading another DAG or `maxMs` without a deploy ends it.
 */
export function watchNextDeploy(
  dagId: string,
  onDeployed: (info: DeployedInfo) => void,
  maxMs = 30 * 60_000,
): () => void {
  cleanup?.();
  const start = useBuilder.getState();
  const startEtag = start.baseEtag;
  const startPr = start.pendingPrUrl;
  const unsubscribe = useBuilder.subscribe((s) => {
    if (s.savedDagId && s.savedDagId !== dagId) {
      stop();
      return;
    }
    if (s.savedDagId === dagId && !s.dirty && (s.baseEtag !== startEtag || s.pendingPrUrl !== startPr)) {
      stop();
      onDeployed({ dagId, etag: s.baseEtag, prUrl: s.pendingPrUrl });
    }
  });
  const timer = setTimeout(() => stop(), maxMs);
  function stop() {
    unsubscribe();
    clearTimeout(timer);
    if (cleanup === stop) cleanup = null;
  }
  cleanup = stop;
  return stop;
}
