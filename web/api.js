// API client: bearer tokens with automatic refresh, JSON helpers, chunked uploads.

const TOKENS = "autoppt.tokens";

export class ApiError extends Error {
  constructor(status, body) {
    super((body && (body.message || body.detail)) || `HTTP ${status}`);
    this.status = status;
    this.body = body || {};
  }
}

export const auth = {
  get() {
    try { return JSON.parse(localStorage.getItem(TOKENS)) || null; } catch { return null; }
  },
  set(tokens) { localStorage.setItem(TOKENS, JSON.stringify(tokens)); },
  clear() { localStorage.removeItem(TOKENS); },
  loggedIn() { return !!this.get(); },
};

let refreshing = null;
const REASONS = "autoppt.accessReasons";

// An administrator opening another user's job must give a reason; it is sent with every call for that job.
export function setAccessReason(jobId, reason) {
  const reasons = JSON.parse(sessionStorage.getItem(REASONS) || "{}");
  reasons[jobId] = reason;
  sessionStorage.setItem(REASONS, JSON.stringify(reasons));
}

function accessReason(path) {
  const match = path.match(/^\/api\/jobs\/([0-9a-f]{32})/);
  if (!match) return null;
  return JSON.parse(sessionStorage.getItem(REASONS) || "{}")[match[1]] || null;
}

async function refresh() {
  const tokens = auth.get();
  if (!tokens) return false;
  refreshing = refreshing || fetch("/api/auth/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: tokens.refresh_token }),
  }).then(async (r) => {
    if (!r.ok) { auth.clear(); return false; }
    auth.set(await r.json());
    return true;
  }).finally(() => { refreshing = null; });
  return refreshing;
}

export async function request(method, path, body, extraHeaders = {}) {
  const send = () => {
    const headers = { ...extraHeaders };
    const tokens = auth.get();
    if (tokens) headers.Authorization = `Bearer ${tokens.access_token}`;
    const reason = accessReason(path);
    if (reason) headers["X-Access-Reason"] = encodeURIComponent(reason);
    if (body !== undefined) headers["Content-Type"] = "application/json";
    return fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  };
  let response = await send();
  if (response.status === 401 && auth.get() && (await refresh())) response = await send();
  if (response.status === 204) return null;
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { message: text }; }
  if (!response.ok) throw new ApiError(response.status, data);
  return data;
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, body, headers) => request("POST", path, body ?? {}, headers),
  put: (path, body) => request("PUT", path, body),
  del: (path) => request("DELETE", path),
};

// Chunked upload: init -> PUT chunks to the signed URL -> complete.  onProgress(sent, total).
export async function uploadFile(file, onProgress = () => {}) {
  const init = await api.post("/api/uploads", { filename: file.name, size: file.size });
  let offset = 0;
  while (offset < file.size) {
    const chunk = file.slice(offset, offset + init.chunk_size);
    const r = await fetch(`${init.upload_url}?offset=${offset}`, { method: "PUT", body: chunk });
    if (!r.ok) throw new ApiError(r.status, await r.json().catch(() => ({})));
    offset += chunk.size;
    onProgress(offset, file.size);
  }
  return api.post(`/api/uploads/${init.upload_id}/complete`);
}
