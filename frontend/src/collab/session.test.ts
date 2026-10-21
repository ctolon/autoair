import { describe, expect, it } from "vitest";

import { collabServerUrl } from "./session";

const at = (href: string) => new URL(href) as unknown as Location;

describe("collabServerUrl", () => {
  it("derives the room server from the API base (plugin, standalone, TLS)", () => {
    expect(collabServerUrl("/autoair/api", at("http://airflow:8080/dags"))).toBe("ws://airflow:8080/autoair/collab");
    expect(collabServerUrl("/api", at("https://autoair.acme.io/ui/"))).toBe("wss://autoair.acme.io/collab");
    expect(collabServerUrl("/sub/autoair/api/", at("http://h/"))).toBe("ws://h/sub/autoair/collab");
  });
});
