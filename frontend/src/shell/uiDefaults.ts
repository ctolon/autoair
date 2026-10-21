/** UI defaults from the server (`ui.default_timezone`, `ui.date_locale`) for code formatting dates. */
interface UiDefaults {
  timeZone?: string;
  locale?: string;
}

let defaults: UiDefaults = {};

/** Time zone / locale configured on the server (empty: the browser's). */
export function uiDefaults(): UiDefaults {
  return defaults;
}

export function setUiDefaults(next: UiDefaults): void {
  defaults = next;
}
