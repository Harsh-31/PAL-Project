const TOKEN_KEY = 'palms_token';

export const getToken = () => localStorage.getItem(TOKEN_KEY);
export const setToken = (t) => localStorage.setItem(TOKEN_KEY, t);
export const clearToken = () => localStorage.removeItem(TOKEN_KEY);

export async function api(path, { method = 'GET', body, headers = {} } = {}) {
  const token = getToken();
  const finalHeaders = { 'Content-Type': 'application/json', ...headers };
  if (token) finalHeaders.Authorization = `Bearer ${token}`;

  const res = await fetch(path, {
    method,
    headers: finalHeaders,
    body: body ? JSON.stringify(body) : undefined,
  });

  const contentType = res.headers.get('content-type') || '';
  const data = contentType.includes('application/json') ? await res.json() : await res.text();

  if (!res.ok) {
    const detail = typeof data === 'object' ? data.detail || JSON.stringify(data) : data;
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return data;
}
