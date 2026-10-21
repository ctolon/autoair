// Ports and paths shared by the capture scripts (override with env vars).
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const HERE = dirname(fileURLToPath(import.meta.url));
export const WEBSITE = resolve(HERE, "../..");
export const ROOT = resolve(WEBSITE, "..");
export const STATE = resolve(HERE, ".state");
export const AIRFLOW = process.env.AIRFLOW_URL ?? "http://localhost:8080";
const API_PORT = Number(process.env.CAPTURE_API_PORT ?? 8860);
const UI_PORT = Number(process.env.CAPTURE_UI_PORT ?? 8861);
export const API = `http://127.0.0.1:${API_PORT}/api`;
export const UI = `http://127.0.0.1:${UI_PORT}`;
export const SCREENS = resolve(WEBSITE, "static/img/screens");
export const VIDEOS = resolve(WEBSITE, "static/video");
