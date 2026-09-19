/* format.js — display formatting only. No function here computes a new figure; each one
 * takes a value the API already returned and changes how it is written on screen
 * (thousands separators, decimal places, percent sign, local date string). UI.md: "No
 * arithmetic in JavaScript beyond formatting."
 */

export function fmtNum(v, opts = {}) {
  if (v === null || v === undefined || v === "") return "-";
  if (typeof v !== "number") return String(v);
  if (!Number.isFinite(v)) return String(v);
  const decimals = opts.decimals ?? (Math.abs(v) >= 100 ? 1 : 4);
  return v.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

export function fmtMoney(v, unit = "k EUR") {
  if (v === null || v === undefined) return "-";
  if (typeof v !== "number" || !Number.isFinite(v)) return String(v);
  return `${v.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} ${unit}`;
}

/** Intl's built-in percent formatter (multiplies by 100 and appends "%" internally — a
 * display formatter, the same category as toLocaleString, not arithmetic this code performs). */
export function fmtPct(v, decimals = 2) {
  if (v === null || v === undefined || typeof v !== "number" || !Number.isFinite(v)) return "-";
  return v.toLocaleString(undefined, { style: "percent", minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

export function fmtDate(iso) {
  if (!iso) return "-";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleString();
  } catch {
    return String(iso);
  }
}

/** Any JSON scalar for a generic table cell: numbers through fmtNum, everything else as text. */
export function fmtCell(v) {
  if (v === null || v === undefined) return "-";
  if (typeof v === "number") return fmtNum(v);
  if (typeof v === "boolean") return v ? "true" : "false";
  if (Array.isArray(v) || typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** Derivation parameter/input keys that name a calendar year, never a quantity. A year
 * is not something you add a thousands separator to - "2,027" is meaningless in a way
 * "2,027 kEUR" is not. Matched by key, never by guessing from the value (a genuine
 * quantity can happen to equal a plausible-looking year). */
const YEAR_PARAM_KEYS = new Set([
  "t", "t0", "year", "window_from", "window_to", "prev_year", "target_year", "train_from", "train_to",
]);

/** Format one parameter/input value for display, given the key it is stored under -
 * for a derivation's `parameters`/`inputs` block (trace nodes, regression points) and
 * for any other table whose columns are named the same way (the backtest per-window
 * parameter table). Not for money or rates, which keep using `fmtMoney`/`fmtPct`
 * untouched wherever they already do.
 *
 * A key in `YEAR_PARAM_KEYS` always renders as a bare integer: no thousands
 * separator, no decimal point, regardless of the value. Any other value that is
 * itself a whole number (a sample count such as `n`, a horizon in years, ...) also
 * renders as a bare integer rather than gaining a spurious ".0000" from fmtNum's
 * default precision - only genuinely fractional numbers (alpha, beta, r_squared,
 * valorization/growth rates, ...) still go through fmtNum's normal decimal rules.
 * Everything else (strings, booleans, objects, non-finite numbers) falls back to
 * fmtCell's existing behaviour. */
export function fmtParam(key, v) {
  if (typeof v === "number" && Number.isFinite(v)) {
    if (YEAR_PARAM_KEYS.has(String(key))) {
      return v.toLocaleString(undefined, { useGrouping: false, minimumFractionDigits: 0, maximumFractionDigits: 0 });
    }
    if (Number.isInteger(v)) {
      return v.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 });
    }
  }
  return fmtCell(v);
}
