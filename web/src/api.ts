export type Status = "success" | "warning" | "error";

export interface Envelope<T> {
  status: Status;
  summary: string;
  data: T;
  next_actions: string[];
  artifacts: string[];
}

export type ApiProblem = {
  code?: string;
  title?: string;
  message?: string;
  impact?: string;
  action?: string;
  component?: string;
  technical_detail?: string;
  retryable?: boolean;
};

export class ApiError extends Error {
  readonly status: number;
  readonly problem: ApiProblem;

  constructor(status: number, problem: ApiProblem | string) {
    const normalized = typeof problem === "string"
      ? { message: problem }
      : problem;
    super(normalized.message ?? normalized.title ?? `请求失败：${status}`);
    this.name = "ApiError";
    this.status = status;
    this.problem = normalized;
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<Envelope<T>> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });
  const raw = await response.text();
  let payload: any = null;
  try { payload = raw ? JSON.parse(raw) : null; } catch { payload = null; }
  if (!response.ok) {
    throw new ApiError(response.status, payload?.detail ?? `请求失败：${response.status}`);
  }
  return payload as Envelope<T>;
}

export const post = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "POST", body: JSON.stringify(body) });

export const put = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "PUT", body: JSON.stringify(body) });
