/**
 * Stage 2 of codegen for the Python SDK (runs standalone from the committed openapi.json).
 *
 * Mirrors `packages/podcast-api-client-js/scripts/generate-client.ts`: it walks the SAME
 * normalized-operation model (tag → resource, operationId → method) so the Python and
 * TypeScript SDKs stay in lockstep, and emits idiomatic Python instead of TypeScript:
 *
 *   1. `src/podengine/_generated/models.py`    — pydantic v2 models materialized from the
 *      spec's inline schemas (the spec has NO `components.schemas`; every schema is inlined,
 *      so nested objects are recursively turned into named models, deduped by structure).
 *   2. `src/podengine/_generated/resources.py` — resource classes with sync + async methods,
 *      keyword-only snake_case signatures, docstrings, a descriptor table and typed returns.
 *
 * Run from the package root:  bun run scripts/generate-python-client.ts
 * The output is then normalized with `ruff format` (see CONTRIBUTING / CI).
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const PKG_ROOT = join(SCRIPT_DIR, '..');
// In the monorepo the canonical spec lives in the TS SDK package; the standalone mirror
// carries its own copy at the package root. Prefer the local copy when present.
const LOCAL_SPEC = join(PKG_ROOT, 'openapi.json');
const SIBLING_SPEC = join(PKG_ROOT, '..', 'podcast-api-client-js', 'openapi.json');
const SPEC_PATH = existsSync(LOCAL_SPEC) ? LOCAL_SPEC : SIBLING_SPEC;
const GENERATED_DIR = join(PKG_ROOT, 'src', 'podengine', '_generated');

// --- spec types (only the bits we read) ------------------------------------
interface SchemaObject {
  type?: string | string[];
  format?: string;
  enum?: unknown[];
  const?: unknown;
  properties?: Record<string, SchemaObject>;
  required?: string[];
  items?: SchemaObject;
  additionalProperties?: boolean | SchemaObject;
  anyOf?: SchemaObject[];
  oneOf?: SchemaObject[];
  allOf?: SchemaObject[];
  not?: SchemaObject;
  description?: string;
  [key: string]: unknown;
}
interface ParameterObject {
  name: string;
  in: 'path' | 'query' | 'header' | 'cookie';
  required?: boolean;
  schema?: SchemaObject;
  description?: string;
}
interface OperationObject {
  operationId: string;
  tags?: string[];
  summary?: string;
  description?: string;
  parameters?: ParameterObject[];
  requestBody?: { required?: boolean; content: Record<string, { schema: SchemaObject }> };
  responses: Record<string, { content?: Record<string, { schema: SchemaObject }> }>;
}
interface OpenApiSpec {
  paths: Record<string, Record<string, OperationObject>>;
}

const HTTP_METHODS = ['get', 'post', 'put', 'patch', 'delete'] as const;

// --- casing helpers ---------------------------------------------------------
const PY_KEYWORDS = new Set([
  'False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await', 'break', 'class', 'continue',
  'def', 'del', 'elif', 'else', 'except', 'finally', 'for', 'from', 'global', 'if', 'import', 'in',
  'is', 'lambda', 'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'try', 'while', 'with', 'yield',
]);

const pascalWord = (s: string): string => {
  const cleaned = s.replace(/[^a-zA-Z0-9]+/g, ' ').trim();
  if (!cleaned) return '';
  return cleaned
    .split(/\s+/)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join('');
};

const snakeCase = (input: string): string => {
  let s = input
    .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1_$2')
    .replace(/[^a-zA-Z0-9]+/g, '_')
    .toLowerCase()
    .replace(/_+/g, '_')
    .replace(/^_|_$/g, '');
  if (!s) s = 'field';
  if (/^[0-9]/.test(s)) s = `_${s}`;
  if (PY_KEYWORDS.has(s)) s = `${s}_`;
  return s;
};

// --- model registry ---------------------------------------------------------
interface ModelDef {
  name: string;
  source: string;
}
const models: ModelDef[] = [];
const modelByStructure = new Map<string, string>(); // canonical schema JSON -> model name
const usedModelNames = new Set<string>();

let usesAny = false;
let usesLiteral = false;
let usesDatetime = false;

const canonical = (value: unknown): string => {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') {
    const obj = value as Record<string, unknown>;
    return `{${Object.keys(obj)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${canonical(obj[k])}`)
      .join(',')}}`;
  }
  return JSON.stringify(value);
};

const uniqueModelName = (hint: string): string => {
  let base = pascalWord(hint) || 'Model';
  if (/^[0-9]/.test(base)) base = `M${base}`;
  let name = base;
  let i = 2;
  while (usedModelNames.has(name)) name = `${base}${i++}`;
  usedModelNames.add(name);
  return name;
};

interface PyType {
  expr: string; // non-null core type expression
  nullable: boolean;
}

const ANY: PyType = { expr: 'Any', nullable: false };

const literalValue = (v: unknown): string =>
  typeof v === 'string' ? JSON.stringify(v) : v === true ? 'True' : v === false ? 'False' : String(v);

/** Split a (possibly array) `type` into its non-null members + a nullability flag. */
const splitType = (t: string | string[] | undefined): { types: string[]; nullable: boolean } => {
  if (t === undefined) return { types: [], nullable: false };
  const arr = Array.isArray(t) ? t : [t];
  return { types: arr.filter((x) => x !== 'null'), nullable: arr.includes('null') };
};

const objectModel = (schema: SchemaObject, hint: string): string => {
  const key = canonical(schema);
  const existing = modelByStructure.get(key);
  if (existing) return existing;

  const name = uniqueModelName(hint);
  // Reserve the structure->name mapping before recursing so self-referential schemas resolve.
  modelByStructure.set(key, name);

  const props = schema.properties ?? {};
  const required = new Set(schema.required ?? []);
  const allowExtra = schema.additionalProperties !== false && schema.additionalProperties !== undefined;

  const fieldLines: string[] = [];
  const usedFieldNames = new Set<string>();
  for (const [wireName, propSchema] of Object.entries(props)) {
    let pyName = snakeCase(wireName);
    while (usedFieldNames.has(pyName)) pyName = `${pyName}_`;
    usedFieldNames.add(pyName);

    const t = mapType(propSchema, `${name}${pascalWord(wireName)}`);
    const isRequired = required.has(wireName);
    const ann = isRequired && !t.nullable ? t.expr : `${t.expr} | None`;
    const needsAlias = pyName !== wireName;

    let line: string;
    if (isRequired) {
      line = needsAlias ? `    ${pyName}: ${ann} = Field(alias=${JSON.stringify(wireName)})` : `    ${pyName}: ${ann}`;
    } else {
      line = needsAlias
        ? `    ${pyName}: ${ann} = Field(default=None, alias=${JSON.stringify(wireName)})`
        : `    ${pyName}: ${ann} = None`;
    }
    fieldLines.push(line);
  }

  const configParts = ['populate_by_name=True'];
  if (allowExtra) configParts.push('extra="allow"');
  const body: string[] = [`class ${name}(BaseModel):`];
  body.push(`    model_config = ConfigDict(${configParts.join(', ')})`);
  if (fieldLines.length > 0) body.push('');
  body.push(...(fieldLines.length > 0 ? fieldLines : ['    pass']));

  // Post-order: the model is appended AFTER its children have been emitted by mapType above.
  models.push({ name, source: body.join('\n') });
  return name;
};

/** Map a JSON schema to a Python type, registering named models as a side effect. */
const mapType = (schema: SchemaObject | undefined, hint: string): PyType => {
  if (!schema || typeof schema !== 'object') {
    usesAny = true;
    return ANY;
  }

  // Unions.
  const union = schema.anyOf ?? schema.oneOf;
  if (union && union.length > 0) {
    let nullable = false;
    const parts: string[] = [];
    union.forEach((sub, idx) => {
      const subSplit = splitType(sub.type);
      if (sub.type === 'null' || (subSplit.types.length === 0 && subSplit.nullable)) {
        nullable = true;
        return;
      }
      const mapped = mapType(sub, `${hint}Variant${idx + 1}`);
      nullable = nullable || mapped.nullable;
      if (!parts.includes(mapped.expr)) parts.push(mapped.expr);
    });
    if (parts.length === 0) {
      usesAny = true;
      return { expr: 'Any', nullable };
    }
    return { expr: parts.join(' | '), nullable };
  }

  // Intersections: merge object members into one model (best-effort; rare in this spec).
  if (schema.allOf && schema.allOf.length > 0) {
    const merged: SchemaObject = { type: 'object', properties: {}, required: [] };
    let allObjects = true;
    for (const sub of schema.allOf) {
      if (sub.properties) Object.assign(merged.properties!, sub.properties);
      if (sub.required) merged.required!.push(...sub.required);
      if (sub.additionalProperties !== undefined && sub.additionalProperties !== false) {
        merged.additionalProperties = sub.additionalProperties;
      }
      const st = splitType(sub.type);
      if (sub.properties === undefined && st.types[0] !== 'object') allObjects = false;
    }
    if (allObjects) return { expr: objectModel(merged, hint), nullable: false };
    usesAny = true;
    return ANY;
  }

  const { types, nullable } = splitType(schema.type);

  // Enums -> Literal (string/number/bool members).
  if (schema.enum && schema.enum.length > 0) {
    const enumNullable = nullable || schema.enum.includes(null);
    const members = schema.enum.filter((v) => v !== null);
    const simple = members.every((v) => ['string', 'number', 'boolean'].includes(typeof v));
    if (simple && members.length > 0) {
      usesLiteral = true;
      return { expr: `Literal[${members.map(literalValue).join(', ')}]`, nullable: enumNullable };
    }
    usesAny = true;
    return { expr: 'Any', nullable: enumNullable };
  }

  if (schema.const !== undefined) {
    usesLiteral = true;
    return { expr: `Literal[${literalValue(schema.const)}]`, nullable };
  }

  const primary = types[0];

  if (primary === 'array') {
    const item = mapType(schema.items, `${hint}Item`);
    const itemExpr = item.nullable ? `${item.expr} | None` : item.expr;
    return { expr: `list[${itemExpr}]`, nullable };
  }

  if (primary === 'object' || (!primary && schema.properties)) {
    if (schema.properties && Object.keys(schema.properties).length > 0) {
      return { expr: objectModel(schema, hint), nullable };
    }
    // Free-form map object.
    const ap = schema.additionalProperties;
    if (ap && typeof ap === 'object') {
      const val = mapType(ap, `${hint}Value`);
      const valExpr = val.nullable ? `${val.expr} | None` : val.expr;
      return { expr: `dict[str, ${valExpr}]`, nullable };
    }
    usesAny = true;
    return { expr: 'dict[str, Any]', nullable };
  }

  if (primary === 'string') {
    if (schema.format === 'date-time') {
      usesDatetime = true;
      return { expr: 'datetime', nullable };
    }
    if (schema.format === 'binary') return { expr: 'bytes', nullable };
    return { expr: 'str', nullable };
  }
  if (primary === 'integer') return { expr: 'int', nullable };
  if (primary === 'number') return { expr: 'float', nullable };
  if (primary === 'boolean') return { expr: 'bool', nullable };
  if (primary === 'null') return { expr: 'None', nullable: true };

  usesAny = true;
  return { expr: 'Any', nullable };
};

// --- normalized operations (parity with the TS generator) -------------------
interface OpParam {
  wire: string;
  py: string;
  type: PyType;
  required: boolean;
}
interface NormalizedOp {
  operationId: string;
  tag: string;
  method: string;
  path: string;
  pathParams: string[];
  queryParams: string[];
  bodyMode: 'none' | 'merge' | 'field';
  binary: boolean;
  hasDataEnvelope: boolean;
  params: OpParam[];
  returnType: PyType | null; // null => no content (None)
  summary?: string;
  description?: string;
}

const buildParam = (wire: string, schema: SchemaObject | undefined, required: boolean, hint: string): OpParam => ({
  wire,
  py: snakeCase(wire),
  type: schema ? mapType(schema, hint) : ANY,
  required,
});

const normalizeOperations = (spec: OpenApiSpec): NormalizedOp[] => {
  const ops: NormalizedOp[] = [];
  for (const [path, pathItem] of Object.entries(spec.paths)) {
    for (const method of HTTP_METHODS) {
      const op = pathItem[method];
      if (!op) continue;

      const tag = op.tags?.[0] ?? 'default';
      const opIdPascal = pascalWord(op.operationId);
      const params: OpParam[] = [];

      const allParams = op.parameters ?? [];
      const pathParams = allParams.filter((p) => p.in === 'path');
      const queryParams = allParams.filter((p) => p.in === 'query');
      for (const p of pathParams) {
        params.push(buildParam(p.name, p.schema, p.required ?? true, `${opIdPascal}${pascalWord(p.name)}`));
      }
      for (const p of queryParams) {
        params.push(buildParam(p.name, p.schema, p.required ?? false, `${opIdPascal}${pascalWord(p.name)}`));
      }

      const body = op.requestBody;
      const bodySchema = body?.content?.['application/json']?.schema;
      const hasBody = Boolean(bodySchema);
      const isObjectBody =
        hasBody && (bodySchema?.type === 'object' || (!bodySchema?.type && !!bodySchema?.properties));
      const bodyMode: NormalizedOp['bodyMode'] = !hasBody ? 'none' : isObjectBody ? 'merge' : 'field';

      if (bodyMode === 'merge' && bodySchema) {
        const required = new Set(bodySchema.required ?? []);
        for (const [wire, sub] of Object.entries(bodySchema.properties ?? {})) {
          params.push(buildParam(wire, sub, required.has(wire), `${opIdPascal}${pascalWord(wire)}`));
        }
      } else if (bodyMode === 'field' && bodySchema) {
        const t = mapType(bodySchema, `${opIdPascal}Body`);
        params.push({ wire: 'body', py: 'body', type: t, required: body?.required ?? true });
      }

      const success = op.responses['200'] ?? op.responses['201'];
      const binary = Boolean(success?.content?.['application/octet-stream']);
      const jsonSchema = success?.content?.['application/json']?.schema;
      const hasDataEnvelope = Boolean(jsonSchema?.properties && 'data' in jsonSchema.properties);

      let returnType: PyType | null;
      if (binary) returnType = { expr: 'bytes', nullable: false };
      else if (!jsonSchema) returnType = null;
      else {
        const dataSchema = hasDataEnvelope ? (jsonSchema.properties!.data as SchemaObject) : jsonSchema;
        returnType = mapType(dataSchema, `${opIdPascal}Response`);
      }

      ops.push({
        operationId: op.operationId,
        tag,
        method: method.toUpperCase(),
        path,
        pathParams: pathParams.map((p) => p.name),
        queryParams: queryParams.map((p) => p.name),
        bodyMode,
        binary,
        hasDataEnvelope,
        params,
        returnType,
        summary: op.summary,
        description: op.description,
      });
    }
  }
  return ops.sort((a, b) => a.tag.localeCompare(b.tag) || a.operationId.localeCompare(b.operationId));
};

// --- emit -------------------------------------------------------------------
const annParam = (p: OpParam): string => {
  const core = p.type.expr;
  const expr = p.required && !p.type.nullable ? core : `${core} | None`;
  return qualifyModels(expr);
};

/** Qualify generated model names with the `models.` namespace so resources.py needs no
 * wildcard import. Builtins / typing tokens (`Any`, `Literal`, `dict`, `list`, ...) are left
 * untouched because they are not in the model-name set. */
let qualifyRe: RegExp | null = null;
const qualifyModels = (expr: string): string => {
  if (usedModelNames.size === 0) return expr;
  if (!qualifyRe) {
    const names = [...usedModelNames].sort((a, b) => b.length - a.length);
    qualifyRe = new RegExp(`\\b(${names.join('|')})\\b`, 'g');
  }
  return expr.replace(qualifyRe, 'models.$1');
};

const returnAnn = (op: NormalizedOp): string => {
  if (!op.returnType) return 'None';
  const expr = op.returnType.nullable ? `${op.returnType.expr} | None` : op.returnType.expr;
  return qualifyModels(expr);
};

const docstring = (op: NormalizedOp, indent: string): string => {
  const lines = [op.summary, op.description].filter((l): l is string => Boolean(l && l.trim()));
  if (lines.length === 0) lines.push(`${op.method} ${op.path}`);
  const text = lines.join('\n\n').replace(/"""/g, '\\"\\"\\"');
  if (!text.includes('\n')) return `${indent}"""${text}"""`;
  const inner = text
    .split('\n')
    .map((l) => (l ? `${indent}${l}` : ''))
    .join('\n');
  return `${indent}"""\n${inner}\n${indent}"""`;
};

const sortedParams = (op: NormalizedOp): OpParam[] =>
  [...op.params].sort((a, b) => (a.required === b.required ? 0 : a.required ? -1 : 1));

const paramsDictLiteral = (op: NormalizedOp): string => {
  if (op.params.length === 0) return 'None';
  const entries = op.params.map((p) => `${JSON.stringify(p.wire)}: ${p.py}`);
  return `{${entries.join(', ')}}`;
};

const methodSource = (op: NormalizedOp, isAsync: boolean): string => {
  const params = sortedParams(op);
  const sigParts = ['self', '*'];
  for (const p of params) {
    sigParts.push(p.required ? `${p.py}: ${annParam(p)}` : `${p.py}: ${annParam(p)} = None`);
  }
  sigParts.push('request_options: RequestOptions | None = None');

  const ret = returnAnn(op);
  const defLine = isAsync
    ? `    async def ${snakeCase(op.operationId)}(\n        ${sigParts.join(',\n        ')},\n    ) -> ${ret}:`
    : `    def ${snakeCase(op.operationId)}(\n        ${sigParts.join(',\n        ')},\n    ) -> ${ret}:`;

  const doc = docstring(op, '        ');
  const call = `self._core.request(_DESCRIPTORS[${JSON.stringify(op.operationId)}], ${paramsDictLiteral(op)}, request_options)`;
  const awaited = isAsync ? `await ${call}` : call;

  let body: string;
  if (op.binary || !op.returnType) {
    // Bytes download or no-content response: pass the transport result straight through.
    body = `        return ${awaited}`;
  } else {
    body = `        return _adapter_${op.operationId}.validate_python(${awaited})`;
  }
  return `${defLine}\n${doc}\n${body}`;
};

const main = (): void => {
  const spec = JSON.parse(readFileSync(SPEC_PATH, 'utf-8')) as OpenApiSpec;
  const ops = normalizeOperations(spec);

  const seen = new Set<string>();
  for (const op of ops) {
    if (seen.has(op.operationId)) throw new Error(`Duplicate operationId in spec: ${op.operationId}`);
    seen.add(op.operationId);
  }

  // --- models.py ---
  const modelImports: string[] = ['from __future__ import annotations', ''];
  const typingNames = ['Any', 'Literal'].filter((n) => (n === 'Any' && usesAny) || (n === 'Literal' && usesLiteral));
  if (usesDatetime) modelImports.push('from datetime import datetime');
  if (typingNames.length > 0) modelImports.push(`from typing import ${typingNames.join(', ')}`);
  modelImports.push('', 'from pydantic import BaseModel, ConfigDict, Field');

  const modelsHeader = `# AUTO-GENERATED by scripts/generate-python-client.ts — DO NOT EDIT BY HAND.
# Regenerate with: bun run scripts/generate-python-client.ts
# ruff: noqa
`;
  const modelsSource = [modelsHeader, modelImports.join('\n'), '', models.map((m) => m.source).join('\n\n\n'), ''].join(
    '\n'
  );

  // --- resources.py ---
  const byTag = new Map<string, NormalizedOp[]>();
  for (const op of ops) {
    const list = byTag.get(op.tag) ?? [];
    list.push(op);
    byTag.set(op.tag, list);
  }
  const tags = [...byTag.keys()].sort();

  const descriptorEntries = ops.map((op) => {
    const fields = [
      `method=${JSON.stringify(op.method)}`,
      `path=${JSON.stringify(op.path)}`,
      `path_params=(${op.pathParams.map((p) => `${JSON.stringify(p)},`).join(' ')})`,
      `query_params=(${op.queryParams.map((p) => `${JSON.stringify(p)},`).join(' ')})`,
      `body=${JSON.stringify(op.bodyMode)}`,
      `binary=${op.binary ? 'True' : 'False'}`,
    ];
    return `    ${JSON.stringify(op.operationId)}: EndpointDescriptor(${fields.join(', ')}),`;
  });

  const adapterLines = ops
    .filter((op) => !op.binary && op.returnType)
    .map((op) => `_adapter_${op.operationId}: TypeAdapter[Any] = TypeAdapter(${returnAnn(op)})`);

  // Sync + async resource classes.
  const syncResourceClasses: string[] = [];
  const asyncResourceClasses: string[] = [];
  const syncFields: string[] = [];
  const asyncFields: string[] = [];
  const syncInits: string[] = [];
  const asyncInits: string[] = [];

  for (const tag of tags) {
    const cls = pascalWord(tag) + 'Resource';
    const acls = 'Async' + pascalWord(tag) + 'Resource';
    const prop = snakeCase(tag);
    const tagOps = byTag.get(tag)!;

    syncFields.push(`    ${prop}: ${cls}`);
    asyncFields.push(`    ${prop}: ${acls}`);
    syncInits.push(`        self.${prop} = ${cls}(core)`);
    asyncInits.push(`        self.${prop} = ${acls}(core)`);

    const syncMethods = tagOps.map((op) => methodSource(op, false)).join('\n\n');
    const asyncMethods = tagOps.map((op) => methodSource(op, true)).join('\n\n');
    syncResourceClasses.push(
      `class ${cls}:\n    """${tag} endpoints."""\n\n    def __init__(self, core: PodEngineCore) -> None:\n        self._core = core\n\n${syncMethods}`
    );
    asyncResourceClasses.push(
      `class ${acls}:\n    """${tag} endpoints (async)."""\n\n    def __init__(self, core: AsyncPodEngineCore) -> None:\n        self._core = core\n\n${asyncMethods}`
    );
  }

  const resourcesHeader = `# AUTO-GENERATED by scripts/generate-python-client.ts — DO NOT EDIT BY HAND.
# Regenerate with: bun run scripts/generate-python-client.ts
# ruff: noqa
from __future__ import annotations

from datetime import datetime  # noqa: F401  (used by generated return-type expressions)
from typing import Any, Literal  # noqa: F401  (used by generated return-type expressions)

from pydantic import TypeAdapter

from podengine._core._client import (
    AsyncPodEngineCore,
    EndpointDescriptor,
    PodEngineCore,
    RequestOptions,
)
from podengine._generated import models
`;

  const descriptorTable = `_DESCRIPTORS: dict[str, EndpointDescriptor] = {\n${descriptorEntries.join('\n')}\n}`;

  const syncClient = `class PodEngine:
    """Pod Engine API client (synchronous).

    Example:
        pe = PodEngine(api_key="...")
        chart = pe.charts.get_latest_chart(chart_type="apple", country="us", category="top podcasts")
    """

${syncFields.join('\n')}

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        source: str = "api",
        headers: dict[str, str] | None = None,
        max_retries: int = 2,
        timeout: float | None = 60.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        core = PodEngineCore(
            api_key=api_key,
            base_url=base_url,
            source=source,
            headers=headers,
            max_retries=max_retries,
            timeout=timeout,
            http_client=http_client,
        )
        self._core = core
${syncInits.join('\n')}

    def close(self) -> None:
        self._core.close()

    def __enter__(self) -> "PodEngine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()`;

  const asyncClient = `class AsyncPodEngine:
    """Pod Engine API client (asynchronous).

    Example:
        pe = AsyncPodEngine(api_key="...")
        chart = await pe.charts.get_latest_chart(chart_type="apple", country="us", category="top podcasts")
    """

${asyncFields.join('\n')}

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        source: str = "api",
        headers: dict[str, str] | None = None,
        max_retries: int = 2,
        timeout: float | None = 60.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        core = AsyncPodEngineCore(
            api_key=api_key,
            base_url=base_url,
            source=source,
            headers=headers,
            max_retries=max_retries,
            timeout=timeout,
            http_client=http_client,
        )
        self._core = core
${asyncInits.join('\n')}

    async def aclose(self) -> None:
        await self._core.aclose()

    async def __aenter__(self) -> "AsyncPodEngine":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()`;

  const resourcesSource = [
    resourcesHeader,
    'import httpx',
    '',
    descriptorTable,
    '',
    adapterLines.join('\n'),
    '',
    syncResourceClasses.join('\n\n\n'),
    '',
    asyncResourceClasses.join('\n\n\n'),
    '',
    syncClient,
    '',
    asyncClient,
    '',
  ].join('\n');

  mkdirSync(GENERATED_DIR, { recursive: true });
  writeFileSync(join(GENERATED_DIR, 'models.py'), modelsSource, 'utf-8');
  writeFileSync(join(GENERATED_DIR, 'resources.py'), resourcesSource, 'utf-8');

  console.log(
    `Generated _generated/{models,resources}.py — ${ops.length} operations across ${tags.length} resources, ${models.length} models`
  );
};

main();
