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
