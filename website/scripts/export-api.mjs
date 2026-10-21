#!/usr/bin/env node
/**
 * Exports the REST API's OpenAPI document and the DagSpec JSON Schema from the working
 * tree into static/, where the site serves them (/openapi.json, /schemas/dag-spec.schema.json)
 * and the API reference page renders them. Needs `uv` and the repository's Python
 * environment; the outputs are committed, so building the site does not need Python.
 */
import {execFileSync} from 'node:child_process';
import {mkdirSync} from 'node:fs';
import {dirname, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, '../..');
const statics = resolve(here, '../static');
mkdirSync(resolve(statics, 'schemas'), {recursive: true});

const run = (args) => execFileSync('uv', ['run', 'autoair', ...args], {cwd: repo, stdio: 'inherit'});
run(['export-openapi', '--output', resolve(statics, 'openapi.json')]);
run(['export-schema', '--output', resolve(statics, 'schemas/dag-spec.schema.json')]);
console.log('export-api: wrote static/openapi.json and static/schemas/dag-spec.schema.json');
