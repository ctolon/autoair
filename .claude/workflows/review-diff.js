export const meta = {
  name: 'review-diff',
  description: 'Review a change with the AutoAir reviewer subagents, then have an independent agent try to refute every finding',
  whenToUse: 'Before handing back or opening a PR for a change that spans several areas. Pass the changed paths as args to keep other agents\' work out of the review.',
  phases: [
    { title: 'Scope', detail: 'changed files and the review areas they touch' },
    { title: 'Review', detail: 'one reviewer subagent per area' },
    { title: 'Verify', detail: 'one skeptic per finding tries to refute it' },
  ],
}

const REVIEWERS = {
  backend: 'backend-reviewer',
  frontend: 'frontend-reviewer',
  security: 'security-reviewer',
  airflow: 'airflow-compat-reviewer',
}

const SCOPE = {
  type: 'object',
  required: ['files', 'areas'],
  properties: {
    files: { type: 'array', items: { type: 'string' } },
    areas: { type: 'array', items: { type: 'string', enum: Object.keys(REVIEWERS) } },
  },
}

const FINDINGS = {
  type: 'object',
  required: ['findings'],
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['severity', 'location', 'problem', 'fix'],
        properties: {
          severity: { type: 'string', enum: ['blocker', 'major', 'minor'] },
          location: { type: 'string', description: 'path:line' },
          problem: { type: 'string' },
          why: { type: 'string' },
          fix: { type: 'string' },
        },
      },
    },
  },
}

const VERDICT = {
  type: 'object',
  required: ['refuted', 'reason'],
  properties: { refuted: { type: 'boolean' }, reason: { type: 'string' } },
}

phase('Scope')
const paths = Array.isArray(args) && args.length ? args : null
const scope = await agent(
  (paths
    ? `The change under review consists of these paths: ${paths.join(', ')}.`
    : 'List the files of the change under review with `git status --short` and `git diff --stat` (include untracked files).') +
    ' Then choose the review areas: "backend" for files under src/, tests/, scripts/ or deploy/;' +
    ' "frontend" for files under frontend/; "security" for auth, iam, api, mcp, ai, kb, storage,' +
    ' net, gateway, codegen, importer, runner, collab or deploy files; "airflow" when the change' +
    ' imports airflow, calls Airflow\'s REST API or changes generated DAG code (codegen/,' +
    ' _compat.py, gateway/). Read the diff only as far as needed to decide.',
  { schema: SCOPE, effort: 'low', label: 'scope' },
)
if (!scope || !scope.files.length || !scope.areas.length) {
  log('Nothing to review.')
  return { files: [], confirmed: [], refuted: [] }
}
log(`${scope.files.length} files; areas: ${scope.areas.join(', ')}`)

const files = scope.files.join('\n')
const perArea = await pipeline(
  scope.areas,
  area =>
    agent(
      `Review this change in your area. Limit yourself to these files; other agents edit the` +
        ` same working tree, so ignore everything else:\n${files}\n` +
        'Report only real problems with path:line; an empty list is a valid answer.',
      { agentType: REVIEWERS[area], schema: FINDINGS, phase: 'Review', label: area },
    ),
  (review, area) =>
    parallel(
      (review ? review.findings : []).map(f => () =>
        agent(
          `A ${area} reviewer reported this finding about an AutoAir change:\n` +
            JSON.stringify(f, null, 2) +
            '\nRead the code at that location and try to refute it: is the problem real, reachable' +
            ' and not already handled elsewhere? Set refuted=true when it is wrong, already handled' +
            ' or not reachable, and when you cannot confirm it from the code.',
          { schema: VERDICT, phase: 'Verify', label: `${area}: ${f.location}` },
        ).then(v => ({ ...f, area, refuted: !v || v.refuted, verdict: v ? v.reason : 'no verdict' })),
      ),
    ),
)

const all = perArea.filter(Boolean).flat().filter(Boolean)
const seen = new Set()
const unique = all.filter(f => {
  const key = `${f.location}|${f.problem}`
  if (seen.has(key)) return false
  seen.add(key)
  return true
})
const order = { blocker: 0, major: 1, minor: 2 }
const confirmed = unique.filter(f => !f.refuted).sort((a, b) => order[a.severity] - order[b.severity])
const refuted = unique.filter(f => f.refuted)
log(`${confirmed.length} confirmed, ${refuted.length} refuted`)
return { files: scope.files, areas: scope.areas, confirmed, refuted }
