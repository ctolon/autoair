import { defineConfig } from "@hey-api/openapi-ts";

// Generated from the backend's OpenAPI document (`npm run codegen`).
// The output is committed; CI regenerates it and fails on any diff.
export default defineConfig({
  input: "./openapi.json",
  output: { path: "src/api/gen" },
  plugins: [
    "@hey-api/client-fetch",
    "@hey-api/typescript",
    { name: "@hey-api/sdk" },
    "@tanstack/react-query",
  ],
});
