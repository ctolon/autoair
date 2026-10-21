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
        const res = await request.delete(`${api}/specs/${encodeURIComponent(dagId)}`, {
          headers: headers ?? (auth ? await auth() : {}),
        });
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
