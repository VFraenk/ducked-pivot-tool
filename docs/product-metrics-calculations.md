# Product metrics — calculation logic

> Reference documentation for the Young / Online / Winback (and Nettovertriebsleistung)
> calculations that powered the former **"Produkte" tab**. The tab itself was removed from
> `pivot_fast.html` (it was a UI experiment), but the metric definitions are kept here so the
> logic can be reused — e.g. in the SQL console, a future report, or a downstream pipeline.

All metrics are computed **per `tsc_pks_product_cluster`**, with a grand-total row, over the
records left after the **PKS filters** (see below). They are plain DuckDB aggregates.

---

## 1. Grouping

One row per product cluster, plus a grand total, via `GROUPING SETS`:

```sql
GROUP BY GROUPING SETS ((tsc_pks_product_cluster), ())
-- GROUPING(tsc_pks_product_cluster) = 1 marks the grand-total row
```

## 2. Filters (mirrored 1:1 from the PKS pivot)

The report used **exactly the PKS pivot's filters** — i.e. the `{col: filter}` map built from
the PKS pivot config (`filters` tile + any field filters): `funk_kenner` / `regio_kenner` /
`hybrid_kenner` ≠ True (NULL kept), `fn_planungshauptgruppe_pk ∈ {ET, DP}`, and the
`td_bb_technik` (Fiber) toggle. These become a single `WHERE` clause via the app's `buildWhere`.

> If reproducing standalone (without the PKS pivot), the default preset is:
> ```sql
> WHERE (funk_kenner  IS NULL OR lower(CAST(funk_kenner  AS VARCHAR)) NOT IN ('true','t','1','ja','yes','wahr'))
>   AND (regio_kenner IS NULL OR lower(CAST(regio_kenner AS VARCHAR)) NOT IN ('true','t','1','ja','yes','wahr'))
>   AND (hybrid_kenner IS NULL OR lower(CAST(hybrid_kenner AS VARCHAR)) NOT IN ('true','t','1','ja','yes','wahr'))
>   AND fn_planungshauptgruppe_pk IN ('ET','DP')
> -- (the Fiber toggle on td_bb_technik is optional / separate)
> ```

## 3. Metrics

Each metric below is an aggregate expression in the `SELECT`. When a source column is missing,
the report emitted `0` so the metric simply shows blank.

### 3.1 Records (count)
```sql
COUNT(*) AS n
```

### 3.2 Nettovertriebsleistung (the measure)
The PKS pivot's first value field. Default:
```sql
SUM(sum_td_nettovertriebsleistung) AS msr
```
Also the basis for the "< X %" share filter (section 4).

### 3.3 Young share — from `fn_young_tarif_text`
Denominator uses **only** the listed values `JA` + `NEIN` (everything else / NULL excluded):
```sql
-- numerator (JA)
COUNT(*) FILTER (WHERE upper(trim(CAST(fn_young_tarif_text AS VARCHAR))) IN ('JA'))         AS young_ja,
-- denominator (JA + NEIN)
COUNT(*) FILTER (WHERE upper(trim(CAST(fn_young_tarif_text AS VARCHAR))) IN ('JA','NEIN'))  AS young_tot
-- Young share = young_ja / young_tot
```

### 3.4 Online — from `kanal`
Denominator is **all non-null channels**; the two online channels are
`INT intern PK` and `INT extern PK`:
```sql
COUNT(*) FILTER (WHERE kanal = 'INT intern PK')  AS int_intern,
COUNT(*) FILTER (WHERE kanal = 'INT extern PK')  AS int_extern,
COUNT(*) FILTER (WHERE kanal IS NOT NULL)        AS kanal_tot
```
Derived shares:
- **Online total** = `(int_intern + int_extern) / kanal_tot`
- **thereof Intern** = `int_intern / kanal_tot`
- **thereof Extern** = `int_extern / kanal_tot`

### 3.5 Winback share — from `winback_kenner`
Denominator uses **only** the listed values `0` + `1` (handles int or boolean encodings):
```sql
-- numerator (winback = 1)
COUNT(*) FILTER (WHERE lower(CAST(winback_kenner AS VARCHAR)) IN ('1','true'))               AS wb_1,
-- denominator (0 + 1)
COUNT(*) FILTER (WHERE lower(CAST(winback_kenner AS VARCHAR)) IN ('0','1','true','false'))   AS wb_tot
-- Winback share = wb_1 / wb_tot
```

## 4. "< X %" filter (full vs. filtered)

The PKS pivot's `hideThresholdPct` ("< X %") was applied as a **share-of-total** filter on the
measure. A product cluster was kept in the *filtered* view when:

```text
abs(cluster.msr) / abs(total.msr) >= hideThresholdPct / 100
```

> Note: at the coarse product-cluster grain (typically only a handful of clusters) this rarely
> hides anything, because each cluster is a large share of the total. This is one reason the tab
> was retired. If a measure share is unusable (all NULL/0), fall back to the record-count share
> (`n`) on the same formula.

## 5. Ordering

Total first, then by technology group, then by tariff size:

```sql
ORDER BY
  GROUPING(tsc_pks_product_cluster) DESC,                       -- total row first
  CASE                                                          -- technology group
    WHEN CAST(tsc_pks_product_cluster AS VARCHAR) ILIKE '%kupfer%'    THEN 0
    WHEN CAST(tsc_pks_product_cluster AS VARCHAR) ILIKE '%fiber_neu%' THEN 1
    WHEN CAST(tsc_pks_product_cluster AS VARCHAR) ILIKE '%fiber_alt%' THEN 2
    ELSE 3
  END,
  CASE lower(regexp_extract(CAST(tsc_pks_product_cluster AS VARCHAR), '[^_]+$'))   -- size s→big
    WHEN 's' THEN 0 WHEN 'm' THEN 1 WHEN 'l' THEN 2 WHEN 'xl' THEN 3 WHEN 'xxl' THEN 4 WHEN 'xxxl' THEN 5 ELSE 9
  END,
  tsc_pks_product_cluster
```

## 6. Full reference query

Drop-in for the SQL console (replace `your_table`; the `WHERE` is the PKS filter — adjust or
remove as needed):

```sql
SELECT
  tsc_pks_product_cluster                                                          AS product,
  GROUPING(tsc_pks_product_cluster)                                                AS is_total,
  COUNT(*)                                                                         AS n,
  SUM(sum_td_nettovertriebsleistung)                                               AS msr,
  COUNT(*) FILTER (WHERE upper(trim(CAST(fn_young_tarif_text AS VARCHAR))) IN ('JA'))        AS young_ja,
  COUNT(*) FILTER (WHERE upper(trim(CAST(fn_young_tarif_text AS VARCHAR))) IN ('JA','NEIN')) AS young_tot,
  COUNT(*) FILTER (WHERE kanal = 'INT intern PK')                                  AS int_intern,
  COUNT(*) FILTER (WHERE kanal = 'INT extern PK')                                  AS int_extern,
  COUNT(*) FILTER (WHERE kanal IS NOT NULL)                                        AS kanal_tot,
  COUNT(*) FILTER (WHERE lower(CAST(winback_kenner AS VARCHAR)) IN ('1','true'))             AS wb_1,
  COUNT(*) FILTER (WHERE lower(CAST(winback_kenner AS VARCHAR)) IN ('0','1','true','false')) AS wb_tot
FROM your_table
WHERE (funk_kenner   IS NULL OR lower(CAST(funk_kenner   AS VARCHAR)) NOT IN ('true','t','1','ja','yes','wahr'))
  AND (regio_kenner  IS NULL OR lower(CAST(regio_kenner  AS VARCHAR)) NOT IN ('true','t','1','ja','yes','wahr'))
  AND (hybrid_kenner IS NULL OR lower(CAST(hybrid_kenner AS VARCHAR)) NOT IN ('true','t','1','ja','yes','wahr'))
  AND fn_planungshauptgruppe_pk IN ('ET','DP')
GROUP BY GROUPING SETS ((tsc_pks_product_cluster), ())
ORDER BY is_total DESC, product;
```

Then derive the displayed shares from the aggregated columns:

| Display              | Formula                                  |
|----------------------|------------------------------------------|
| Young share          | `young_ja / young_tot`                   |
| Online total         | `(int_intern + int_extern) / kanal_tot`  |
| thereof Intern       | `int_intern / kanal_tot`                 |
| thereof Extern       | `int_extern / kanal_tot`                 |
| Winback share        | `wb_1 / wb_tot`                          |
| Nettovertriebsleistung | `msr` (and share = `msr / total msr`)  |
