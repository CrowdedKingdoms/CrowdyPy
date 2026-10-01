// Regenerates CrowdyPy's player-host schemas and its validation fixture from a CrowdyJS
// checkout at the pinned commit, built (`npm ci && npm run build`):
//
//   node tools/fixtures/player_host.mjs ../CrowdyJS
//
// Writes src/crowdypy/player_host/schemas.v1.json (CrowdyJS's schema objects, verbatim) and
// tests/fixtures/player-host.v1.json (values and schemas with CrowdyJS's verdicts on them).
import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const crowdyjs = resolve(process.argv[2] ?? join(root, '..', 'CrowdyJS'));
const pyproject = readFileSync(join(root, 'pyproject.toml'), 'utf8');
const pin = /\[tool\.crowdypy\.crowdyjs\][^[]*?commit\s*=\s*"([0-9a-f]{40})"/s.exec(pyproject)?.[1];
const head = execFileSync('git', ['-C', crowdyjs, 'rev-parse', 'HEAD']).toString().trim();
if (!pin || head !== pin) {
  throw new Error(`CrowdyJS is at ${head}; CrowdyPy pins ${pin}. Check out the pin and build it.`);
}
const version = JSON.parse(readFileSync(join(crowdyjs, 'package.json'), 'utf8')).version;
const ph = await import(pathToFileURL(join(crowdyjs, 'dist', 'player-host', 'index.js')).href);
const crowdyJs = { version, commit: pin };

const SCHEMA_NAMES = [
  'GAME_COMMAND_RESULT_SCHEMA_V1',
  'GAME_COMMAND_SCHEMAS_V1',
  'GAME_COMMAND_SCHEMA_V1',
  'GAME_OBSERVATION_SCHEMA_V1',
  'OBSERVE_REQUEST_SCHEMA_V1',
  'PLAYER_HOST_CAPABILITIES_SCHEMA_V1',
];
const schemas = Object.fromEntries(SCHEMA_NAMES.map((name) => [name, ph[name]]));

const v3 = (x = '1', y = '2', z = '3') => ({ x, y, z });
const planned = { observationId: 'obs-1', capabilityRevision: 'rev-1', controlledEntityId: 'ent-1' };
const observation = {
  contractVersion: 'crowdy.game-observation/1',
  observationId: 'obs-1',
  capabilityRevision: 'rev-1',
  controlledEntityId: 'ent-1',
  observedAt: '2026-09-30T12:00:00Z',
  expiresAt: '2026-09-30T12:00:05.123Z',
  player: { position: v3(), velocity: v3('0', '0', '0'), look: { yaw: '90', pitch: '-10.5' }, health: '20', alive: true },
  controlledEntity: { kind: 'PLAYER', position: v3(), velocity: v3('0', '0', '0') },
  target: { targetId: 'a-1', kind: 'ACTOR', distance: '3' },
  inventory: { selectedSlot: 0, slots: [{ slot: 0, itemId: 'pickaxe', quantity: 1, usable: true }], craftableRecipeIds: ['torch'] },
  grid: { gridRef: 'grid-1', low: v3('0', '0', '0'), high: v3('15', '15', '15'), effectiveScopes: ['observe', 'locomotion'] },
  nearbyActors: [{ actorId: 'a-1', kind: 'MOB', position: v3('4', '2', '3'), distance: '3', disposition: 'HOSTILE', label: 'Zombie', health: '7' }],
  nearbyVoxels: [{ position: v3('1', '1', '1'), material: 'stone', interaction: 'MINE' }],
  inputState: { modalOpen: false, textInputFocused: false, humanInputActive: false },
};
const capabilities = {
  contractVersion: 'crowdy.player-host/1',
  gameId: 'game-1',
  revision: 'rev-1',
  controlledEntityId: 'ent-1',
  commands: [{ kind: 'MOVE', toolName: 'game_move', requiredScope: 'locomotion', risk: 'WORLD_CONTROL', approval: 'NONE', rateLimitPerSecond: 10 }],
  observation: { maxAgeMs: 1000, maxNearbyActors: 32, maxNearbyVoxels: 64 },
  advertisedAt: '2026-09-30T12:00:00Z',
};
const commands = [
  { kind: 'MOVE', ...planned, direction: 'FORWARD', intensity: 0.5, durationMs: 250 },
  { kind: 'LOOK', ...planned, deltaYaw: 15, deltaPitch: -5 },
  { kind: 'STOP' },
  { kind: 'INVENTORY_SELECT', ...planned, slot: 2 },
  { kind: 'INVENTORY_CONSUME', ...planned, slot: 2, quantity: 1 },
  { kind: 'INVENTORY_TRANSFER', ...planned, direction: 'TO_CONTAINER', slot: 1, quantity: 3, containerRef: 'chest-1' },
  { kind: 'INTERACT', ...planned, action: 'MINE', targetRef: 'voxel-1', inventorySlot: 0 },
  { kind: 'CRAFT', ...planned, recipeId: 'torch', quantity: 4 },
  { kind: 'MOUNT', ...planned, action: 'MOUNT', mountRef: 'horse-1' },
  { kind: 'COMBAT_ATTACK', ...planned, targetRef: 'a-1', attack: 'PRIMARY' },
  { kind: 'CHAT_SEND', ...planned, channel: 'LOCAL', text: 'hello' },
  { kind: 'TRAVEL_TELEPORT', ...planned, destinationRef: 'spawn' },
];
const result = { contractVersion: 'crowdy.game-command-result/1', status: 'SUCCEEDED', commandKind: 'MOVE', observationId: 'obs-1', details: [{ name: 'moved', value: '1' }] };

const clone = (value) => JSON.parse(JSON.stringify(value));
const without = (value, key) => { const copy = clone(value); delete copy[key]; return copy; };
const withValue = (value, path, next) => {
  const copy = clone(value);
  let node = copy;
  for (const key of path.slice(0, -1)) node = node[key];
  node[path.at(-1)] = next;
  return copy;
};

const valueCases = [];
const add = (name, schema, value, options) => valueCases.push({ name, schema, value, ...(options ? { options } : {}) });

add('observation', 'GAME_OBSERVATION_SCHEMA_V1', observation);
for (const key of Object.keys(observation)) add(`observation without ${key}`, 'GAME_OBSERVATION_SCHEMA_V1', without(observation, key));
add('observation unknown field', 'GAME_OBSERVATION_SCHEMA_V1', { ...observation, userId: '7' });
add('observation bad date', 'GAME_OBSERVATION_SCHEMA_V1', withValue(observation, ['observedAt'], '2026-13-01T00:00:00Z'));
add('observation local time', 'GAME_OBSERVATION_SCHEMA_V1', withValue(observation, ['observedAt'], '2026-09-30T12:00:00+02:00'));
add('observation nanoseconds', 'GAME_OBSERVATION_SCHEMA_V1', withValue(observation, ['observedAt'], '2026-09-30T12:00:00.123456789Z'));
add('observation non-decimal coordinate', 'GAME_OBSERVATION_SCHEMA_V1', withValue(observation, ['player', 'position', 'x'], '1e3'));
add('observation coordinate newline', 'GAME_OBSERVATION_SCHEMA_V1', withValue(observation, ['player', 'position', 'x'], '12\n'));
add('observation negative zero coordinate', 'GAME_OBSERVATION_SCHEMA_V1', withValue(observation, ['player', 'position', 'x'], '-0'));
add('observation alive as number', 'GAME_OBSERVATION_SCHEMA_V1', withValue(observation, ['player', 'alive'], 1));
add('observation bad enum', 'GAME_OBSERVATION_SCHEMA_V1', withValue(observation, ['controlledEntity', 'kind'], 'BOAT'));
add('observation slot as float', 'GAME_OBSERVATION_SCHEMA_V1', withValue(observation, ['inventory', 'selectedSlot'], 1.5));
add('observation slot negative', 'GAME_OBSERVATION_SCHEMA_V1', withValue(observation, ['inventory', 'selectedSlot'], -1));
add('observation long label', 'GAME_OBSERVATION_SCHEMA_V1', withValue(observation, ['nearbyActors', 0, 'label'], 'x'.repeat(5000)));
add('observation astral label', 'GAME_OBSERVATION_SCHEMA_V1', withValue(observation, ['nearbyActors', 0, 'label'], '\u{1F600}'.repeat(40)));
add('observation duplicate scopes', 'GAME_OBSERVATION_SCHEMA_V1', withValue(observation, ['grid', 'effectiveScopes'], ['observe', 'observe']));
add('observation many actors', 'GAME_OBSERVATION_SCHEMA_V1', withValue(observation, ['nearbyActors'], Array.from({ length: 300 }, () => observation.nearbyActors[0])));
add('observation as array', 'GAME_OBSERVATION_SCHEMA_V1', [observation]);
add('observation tight node budget', 'GAME_OBSERVATION_SCHEMA_V1', observation, { maxNodes: 20 });
add('observation tight depth', 'GAME_OBSERVATION_SCHEMA_V1', observation, { maxDepth: 1, direction: 'OUTPUT' });
add('observation tight bytes', 'GAME_OBSERVATION_SCHEMA_V1', observation, { maxBytes: 100 });
add('capabilities', 'PLAYER_HOST_CAPABILITIES_SCHEMA_V1', capabilities);
add('capabilities wrong contract', 'PLAYER_HOST_CAPABILITIES_SCHEMA_V1', withValue(capabilities, ['contractVersion'], 'crowdy.player-host/2'));
add('capabilities huge rate', 'PLAYER_HOST_CAPABILITIES_SCHEMA_V1', withValue(capabilities, ['commands', 0, 'rateLimitPerSecond'], 1e9));
add('observe request', 'OBSERVE_REQUEST_SCHEMA_V1', { detail: 'STANDARD', maxNearbyActors: 16, maxNearbyVoxels: 32 });
add('observe request integral float', 'OBSERVE_REQUEST_SCHEMA_V1', { detail: 'STANDARD', maxNearbyActors: 16.0, maxNearbyVoxels: 32 });
add('observe request bad detail', 'OBSERVE_REQUEST_SCHEMA_V1', { detail: 'FULL', maxNearbyActors: 16, maxNearbyVoxels: 32 });
for (const command of commands) {
  add(`command ${command.kind}`, 'GAME_COMMAND_SCHEMA_V1', command);
  add(`command ${command.kind} alone`, { $ref: `GAME_COMMAND_SCHEMAS_V1.${command.kind}` }, command);
}
add('command unknown kind', 'GAME_COMMAND_SCHEMA_V1', { kind: 'FLY', ...planned });
add('command move out of range', 'GAME_COMMAND_SCHEMA_V1', { ...commands[0], intensity: 3 });
add('command chat too long', 'GAME_COMMAND_SCHEMA_V1', { ...commands[10], text: 'y'.repeat(4000) });
add('command stop with extra', 'GAME_COMMAND_SCHEMA_V1', { kind: 'STOP', observationId: 'obs-1' });
add('result', 'GAME_COMMAND_RESULT_SCHEMA_V1', result);
add('result with error', 'GAME_COMMAND_RESULT_SCHEMA_V1', { ...result, status: 'FAILED', error: { code: 'AGENT_TOOL_FAILED', message: 'no', retryable: false } });
add('result bad error code', 'GAME_COMMAND_RESULT_SCHEMA_V1', { ...result, status: 'FAILED', error: { code: 'NOPE', message: 'no', retryable: false } });
add('inline const', { type: 'number', minimum: -10, maximum: 10, const: 0 }, 0);
add('inline const mismatch', { type: 'string', maxLength: 4, const: 'a' }, 'b');
add('inline enum bool vs number', { type: 'number', minimum: 0, maximum: 2, enum: [1] }, 1);
add('inline oneOf ambiguous', { oneOf: [{ type: 'string', maxLength: 5 }, { type: 'string', maxLength: 9 }] }, 'abc');
add('inline uuid', { type: 'string', maxLength: 64, format: 'uuid' }, '123e4567-e89b-42d3-a456-426614174000');
add('inline bad uuid', { type: 'string', maxLength: 64, format: 'uuid' }, '123e4567-e89b-02d3-a456-426614174000');
add('inline big integer', { type: 'integer', minimum: 0, maximum: 1e300 }, 2 ** 60);
for (const when of ['2026-02-31T00:00:00Z', '2026-02-32T00:00:00Z', '2026-00-10T00:00:00Z', '2026-01-00T00:00:00Z', '0000-01-01T00:00:00Z', '2026-01-01T24:00:00Z', '2026-01-01T24:00:00.000Z', '2026-01-01T24:00:00.5Z', '2026-01-01T24:00:01Z', '2026-01-01T23:59:60Z', '2026-01-01T00:60:00Z', '2026-01-01T25:00:00Z']) {
  add(`date-time ${when}`, { type: 'string', maxLength: 64, format: 'date-time' }, when);
}
for (const n of [0.000001, 1.5e-7, 2 ** 60, 1e21, 123456789.25]) {
  add(`range message ${n}`, { type: 'number', minimum: -1, maximum: 1e-9 }, n);
}
add('inline unique objects', { type: 'array', maxItems: 4, uniqueItems: true, items: { type: 'object', additionalProperties: false, required: [], properties: { a: { type: 'number', minimum: 0, maximum: 9 }, b: { type: 'number', minimum: 0, maximum: 9 } } } }, [{ a: 1, b: 2 }, { b: 2, a: 1 }]);

const resolveSchema = (schema) => {
  if (typeof schema === 'string') return ph[schema];
  if (schema.$ref) {
    const [base, key] = schema.$ref.split('.');
    return ph[base][key];
  }
  return schema;
};
const verdict = (run) => {
  try {
    run();
    return { ok: true };
  } catch (error) {
    if (!(error instanceof ph.CrowdyAgentError)) throw error;
    return { ok: false, error: error.toJSON() };
  }
};
for (const entry of valueCases) {
  entry.expected = verdict(() => ph.validateJsonSchemaValue(resolveSchema(entry.schema), entry.value, entry.options));
}

const schemaCases = [];
for (const name of ['GAME_COMMAND_RESULT_SCHEMA_V1', 'GAME_OBSERVATION_SCHEMA_V1', 'OBSERVE_REQUEST_SCHEMA_V1', 'PLAYER_HOST_CAPABILITIES_SCHEMA_V1']) {
  schemaCases.push({ name, schema: name });
}
for (const kind of Object.keys(ph.GAME_COMMAND_SCHEMAS_V1)) {
  schemaCases.push({ name: `command ${kind}`, schema: { $ref: `GAME_COMMAND_SCHEMAS_V1.${kind}` } });
}
const object = (properties, extra = {}) => ({ type: 'object', additionalProperties: false, required: [], properties, ...extra });
schemaCases.push(
  { name: 'open object', schema: { type: 'object', additionalProperties: true, required: [], properties: {} } },
  { name: 'authority field', schema: object({ user_id: { type: 'string', maxLength: 9 } }) },
  { name: 'authority field allowed', schema: object({ user_id: { type: 'string', maxLength: 9 } }), options: { rejectAuthorityFields: false } },
  { name: 'unbounded string', schema: object({ text: { type: 'string' } }) },
  { name: 'unbounded number', schema: object({ n: { type: 'number', minimum: 0 } }) },
  { name: 'unbounded array', schema: object({ list: { type: 'array', items: { type: 'null' } } }) },
  { name: 'duplicate enum', schema: object({ e: { type: 'string', maxLength: 3, enum: ['a', 'a'] } }) },
  { name: 'empty enum', schema: object({ e: { type: 'string', maxLength: 3, enum: [] } }) },
  { name: 'single oneOf', schema: object({ u: { oneOf: [{ type: 'null' }] } }) },
  { name: 'missing required schema', schema: object({ a: { type: 'null' } }, { required: ['b'] }) },
  { name: 'duplicate required', schema: object({ a: { type: 'null' } }, { required: ['a', 'a'] }) },
  { name: 'bad pattern', schema: object({ p: { type: 'string', maxLength: 3, pattern: '(' } }) },
  { name: 'max properties too big', schema: object({ a: { type: 'null' } }, { maxProperties: 2 }) },
);
for (const entry of schemaCases) {
  entry.expected = verdict(() => ph.assertBoundedJsonSchema(resolveSchema(entry.schema), entry.options));
}

const errorCases = [
  { code: 'AGENT_TOOL_FAILED', message: 'Bearer abc.def failed, api_key=xyz; token : q and secret=s3' },
  { code: 'AGENT_TOOL_FAILED', message: 'tokenizer and xtoken stay; Token\u3000value goes' },
  { code: 'AGENT_TOOL_FAILED', message: 'line\u0001break\u007fend\nkept\ttab' },
  { code: 'AGENT_TOOL_FAILED', message: '   ' },
  { code: 'AGENT_TOOL_FAILED', message: `${'long '.repeat(150)}\u{1F600}`, options: { remediation: 'apikey: hunter2', field: 'f'.repeat(300), requiredScope: 's'.repeat(100), retryable: true } },
].map((entry) => ({ ...entry, expected: new ph.CrowdyAgentError(entry.code, entry.message, entry.options).toJSON() }));

const canonicalCases = [
  { name: 'nested', value: { b: [1, 'é', null, true], a: { d: -0, c: 2.5 } } },
  { name: 'numbers', value: [0.000001, 1.5e-7, 2 ** 60, 1e21, 123456789.25, -1e-7, 5] },
  { name: 'utf16 key order', value: { '\u{1F600}': 1, '\uff5a': 2, z: 3 } },
].map((entry) => ({ ...entry, canonical: ph.canonicalJson(entry.value), digest: ph.digestCanonicalJson(entry.value) }));

const fixture = {
  fixtureVersion: 1,
  crowdyJs,
  errorCodes: [...ph.CROWDY_AGENT_ERROR_CODES],
  values: valueCases,
  schemas: schemaCases,
  errors: errorCases,
  canonical: canonicalCases,
};
writeFileSync(join(root, 'src/crowdypy/player_host/schemas.v1.json'), `${JSON.stringify({ crowdyJs, schemas }, null, 2)}\n`);
writeFileSync(join(root, 'tests/fixtures/player-host.v1.json'), `${JSON.stringify(fixture, null, 2)}\n`);
console.log(
  `player_host: ${valueCases.length} values (${valueCases.filter((c) => c.expected.ok).length} valid), ` +
    `${schemaCases.length} schemas, ${errorCases.length} errors from CrowdyJS ${version}`,
);
