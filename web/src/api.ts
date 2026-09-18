export type Status = "success" | "warning" | "error";

export interface Envelope<T> {
  status: Status;
  summary: string;
  data: T;
  next_actions: string[];
  artifacts: string[];
}

export async function api<T>(path: string, init?: RequestInit): Promise<Envelope<T>> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.detail ?? `请求失败：${response.status}`);
  }
  return payload as Envelope<T>;
}

export const post = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "POST", body: JSON.stringify(body) });

export const put = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "PUT", body: JSON.stringify(body) });
