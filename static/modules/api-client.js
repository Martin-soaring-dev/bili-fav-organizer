/* Shared API transport for gradually extracted UI modules.
 * Keep the existing app.js API wrapper until migration is covered by tests.
 */
export function createApiClient({ token = "", onError = null } = {}) {
  return async function request(method, url, body, { timeoutMs = 30000 } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const headers = {};
      if (token) headers["X-BiliFav-Token"] = token;
      if (body !== undefined) headers["Content-Type"] = "application/json";
      const response = await fetch(url, {
        method, headers, signal: controller.signal,
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      const text = await response.text();
      let payload;
      try { payload = text ? JSON.parse(text) : null; }
      catch { payload = text; }
      if (!response.ok) {
        const error = new Error(payload?.error || payload?.detail || `HTTP ${response.status}`);
        error.status = response.status;
        error.payload = payload;
        onError?.(error);
        throw error;
      }
      return payload;
    } finally {
      clearTimeout(timer);
    }
  };
}
