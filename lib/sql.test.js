import { describe, it, expect } from 'vitest';
import { qid, qlit, classifyType, safeJoin, buildWhere } from './sql.js';

describe('qid / qlit (SQL escaping)', () => {
  it('quotes identifiers and escapes embedded quotes', () => {
    expect(qid('amount')).toBe('"amount"');
    expect(qid('we"ird')).toBe('"we""ird"');
  });
  it('escapes literals by type', () => {
    expect(qlit(null)).toBe('NULL');
    expect(qlit(42)).toBe('42');
    expect(qlit(true)).toBe('TRUE');
    expect(qlit("O'Brien")).toBe("'O''Brien'");
  });
});

describe('classifyType', () => {
  it('maps DuckDB types to kinds', () => {
    expect(classifyType('BIGINT')).toBe('num');
    expect(classifyType('DECIMAL(10,2)')).toBe('num');
    expect(classifyType('TIMESTAMP')).toBe('date');
    expect(classifyType('BOOLEAN')).toBe('bool');
    expect(classifyType('VARCHAR')).toBe('text');
  });
});

describe('safeJoin', () => {
  it('renders nulls distinctly so key collisions cannot happen', () => {
    expect(safeJoin(['a', null, 'b'])).toBe('a|__NULL__|b');
    expect(safeJoin([null]) === safeJoin([''])).toBe(false);
  });
});

describe('buildWhere', () => {
  it('returns empty string when there are no filters', () => {
    expect(buildWhere(null, {})).toBe('');
  });
  it('builds a values IN clause with optional blanks', () => {
    expect(buildWhere(null, { region: { type: 'values', included: new Set(['EU', 'US']) } }))
      .toBe('WHERE ("region" IN (\'EU\',\'US\'))');
    expect(buildWhere(null, { region: { type: 'values', included: new Set(['EU']), includeBlank: true } }))
      .toBe('WHERE ("region" IN (\'EU\') OR "region" IS NULL)');
  });
  it('builds numeric, between and negated text clauses', () => {
    expect(buildWhere(null, { amt: { type: 'num', op: '>=', a: '10' } })).toBe('WHERE "amt" >= 10');
    expect(buildWhere(null, { amt: { type: 'num', op: 'between', a: '1', b: '9' } })).toBe('WHERE "amt" BETWEEN 1 AND 9');
    expect(buildWhere(null, { name: { type: 'text', op: 'contains', a: 'x', not: true } }))
      .toBe('WHERE NOT ("name"::VARCHAR ILIKE \'%x%\')');
  });
  it('combines multiple filters with AND', () => {
    const w = buildWhere(null, {
      a: { type: 'num', op: '>', a: '5' },
      b: { type: 'text', op: 'eq', a: 'z' },
    });
    expect(w).toBe('WHERE "a" > 5 AND "b" = \'z\'');
  });
});
