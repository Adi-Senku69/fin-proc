/* api.js — the only place this UI talks to the network.
 *
 * Every call returns a plain result object, never throws:
 *   { ok: true,  status, data,           message: "" }
 *   { ok: false, status, data: null,     message: "<what went wrong>" }
 *
 * status is 0 for a network failure (fetch itself rejected — server down, offline, CORS).
 * Views render `status` + `message` directly (UI.md: "Handle every fetch failure visibly").
 * This module does no arithmetic and invents no data; it only moves bytes and reports what
 * happened.
 */

async function request(method, path, body) {
  let res;
  try {
    const opts = { method };
    if (body !== undefined) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    res = await fetch(path, opts);
  } catch (err) {
    return { ok: false, status: 0, data: null, message: `network error: ${err && err.message ? err.message : err}` };
  }

  let raw = "";
  try {
    raw = await res.text();
  } catch (err) {
    return { ok: false, status: res.status, data: null, message: `could not read response body: ${err}` };
  }

  let data = null;
  if (raw) {
    try {
      data = JSON.parse(raw);
    } catch {
      data = raw; // not JSON — keep the raw text so the message can show it
    }
  }

  if (!res.ok) {
    let message = res.statusText || `HTTP ${res.status}`;
    if (data && typeof data === "object" && "detail" in data) {
      message = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } else if (typeof data === "string" && data) {
      message = data;
    }
    return { ok: false, status: res.status, data, message };
  }

  return { ok: true, status: res.status, data, message: "" };
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, body) => request("POST", path, body),

  /** Plain-text fetch (the `?format=text` trace endpoints). Same result shape, `data` is text. */
  async getText(path) {
    let res;
    try {
      res = await fetch(path);
    } catch (err) {
      return { ok: false, status: 0, data: null, message: `network error: ${err && err.message ? err.message : err}` };
    }
    const text = await res.text();
    if (!res.ok) return { ok: false, status: res.status, data: null, message: text || res.statusText };
    return { ok: true, status: res.status, data: text, message: "" };
  },
};
