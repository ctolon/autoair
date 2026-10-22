import type { APIRequestContext } from "@playwright/test";

/**
 * DAGs a spec deployed, deleted again in its `afterAll` (best effort): the specs run against
 * shared or long-lived servers, and a DAG left behind shows up in every later run and crawl.
 *
 *   const dags = new DeployedDags("crawl");
 *   dags.track(`${api}`, dagId, headers);   // right after a deploy
 *   test.afterAll(({ request }) => dags.cleanup(request));
 *
 * `api` is the AutoAir API base (`/autoair/api`, `http://host:port/api`). DAGs tracked without
 * headers use `auth()` (e.g. a fresh Airflow token) when it is given. Every DAG that could not
 * be deleted is logged with the reason; cleanup itself never fails the run.
 */
export class DeployedDags {
  private readonly items = new Map<string, { api: string; dagId: string; headers?: Record<string, string> }>();

  constructor(private readonly label: string) {}

  track(api: string, dagId: string, headers?: Record<string, string>): void {
    this.items.set(`${api} ${dagId}`, headers ? { api, dagId, headers } : { api, dagId });
  }

  async cleanup(request: APIRequestContext, auth?: () => Promise<Record<string, string>>): Promise<void> {
    const left: string[] = [];
    for (const [key, { api, dagId, headers }] of this.items) {
      try {
        const url = `${api}/specs/${encodeURIComponent(dagId)}`;
        const sent = headers ?? (auth ? await auth() : {});
        let res = await request.delete(url, { headers: sent });
        // 428: deleting needs the stored version's etag (If-Match); the answer names it.
        const etag = res.status() === 428 ? res.headers()["etag"] : undefined;
        if (etag) res = await request.delete(url, { headers: { ...sent, "If-Match": etag } });
        // 404: never stored (a refused deploy) or already removed by the test itself.
        if (res.ok() || res.status() === 404) this.items.delete(key);
        else left.push(`${dagId} (${api}): HTTP ${res.status()} ${(await res.text()).slice(0, 200)}`);
      } catch (e) {
        left.push(`${dagId} (${api}): ${String(e).slice(0, 200)}`);
      }
    }
    if (left.length) console.warn(`[${this.label}] DAGs left behind, delete them by hand:\n  ${left.join("\n  ")}`);
  }
}
