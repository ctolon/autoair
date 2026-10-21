/**
 * Where the site is published and which repository it documents. Change these (or set the
 * environment variables) when the project moves; everything else derives from them.
 */
export const GITHUB_OWNER = process.env.DOCS_GITHUB_OWNER ?? 'ctolon';
export const GITHUB_REPO = process.env.DOCS_GITHUB_REPO ?? 'autoair';
export const REPO_URL = `https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}`;
/** Default branch used for "Edit this page" links. */
export const EDIT_BRANCH = process.env.DOCS_EDIT_BRANCH ?? 'main';
/** Public origin of the site (GitHub Pages by default). */
export const SITE_URL = process.env.DOCS_URL ?? `https://${GITHUB_OWNER}.github.io`;
/** Path the site is served under: `/<repo>/` on GitHub project pages, `/` on a custom domain. */
export const BASE_URL = process.env.DOCS_BASE_URL ?? `/${GITHUB_REPO}/`;
