// Pure SQL-building helpers, extracted verbatim from pivot_fast.html.
// Framework-agnostic and side-effect free — the target shape for Phase 1.

export function qid(name) {
  return '"' + String(name).replace(/"/g, '""') + '"';
}

export function qlit(v) {
  if (v === null || v === undefined) return 'NULL';
  if (typeof v === 'number' || typeof v === 'bigint') return String(v);
  if (typeof v === 'boolean') return v ? 'TRUE' : 'FALSE';
  if (v instanceof Date) return "TIMESTAMP '" + v.toISOString().replace('T', ' ').replace('Z', '') + "'";
  return "'" + String(v).replace(/'/g, "''") + "'";
}

export function classifyType(t) {
  const u = t.toUpperCase();
  if (/INT|DECIMAL|DOUBLE|FLOAT|REAL|HUGEINT|NUMERIC|NUMBER/.test(u)) return 'num';
  if (/DATE|TIME|TIMESTAMP/.test(u)) return 'date';
  if (/BOOL/.test(u)) return 'bool';
  return 'text';
}

export const safeJoin = (arr) =>
  arr.map((v) => (v === null || v === undefined) ? '__NULL__' : String(v)).join('|');

// Builds the WHERE clause from a column->filter map. Mirrors the app's filter model.
export function buildWhere(table, filters) {
  const clauses = [];
  for (const [col, f] of Object.entries(filters || {})) {
    if (!f) continue;
    const c = qid(col);
    if (f.type === 'values') {
      if (!f.included && f.includeBlank) continue;
      const vals = f.included ? [...f.included] : [];
      const parts = [];
      if (vals.length > 0) parts.push(`${c} IN (${vals.map((v) => qlit(v)).join(',')})`);
      if (f.includeBlank) parts.push(`${c} IS NULL`);
      clauses.push(parts.length === 0 ? 'FALSE' : '(' + parts.join(' OR ') + ')');
    } else if (f.type === 'num') {
      if (f.op === 'between') clauses.push(`${c} BETWEEN ${qlit(+f.a)} AND ${qlit(+f.b)}`);
      else if (['>', '<', '=', '>=', '<=', '!='].includes(f.op)) clauses.push(`${c} ${f.op} ${qlit(+f.a)}`);
    } else if (f.type === 'text') {
      const a = String(f.a ?? '');
      let textClause = null;
      if (f.op === 'contains') textClause = `${c}::VARCHAR ILIKE ${qlit('%' + a + '%')}`;
      else if (f.op === 'begins') textClause = `${c}::VARCHAR ILIKE ${qlit(a + '%')}`;
      else if (f.op === 'ends') textClause = `${c}::VARCHAR ILIKE ${qlit('%' + a)}`;
      else if (f.op === 'eq') textClause = `${c} = ${qlit(a)}`;
      else if (f.op === 'regex') textClause = `regexp_matches(${c}::VARCHAR, ${qlit(a)})`;
      if (textClause) clauses.push(f.not ? `NOT (${textClause})` : textClause);
    } else if (f.type === 'date') {
      const lit = (s) => `CAST(${qlit(s)} AS TIMESTAMP)`;
      if (f.op === 'between') clauses.push(`${c} BETWEEN ${lit(f.a)} AND ${lit(f.b)}`);
      else if (['>', '<', '=', '>=', '<=', '!='].includes(f.op)) clauses.push(`${c} ${f.op} ${lit(f.a)}`);
    }
  }
  return clauses.length ? 'WHERE ' + clauses.join(' AND ') : '';
}
