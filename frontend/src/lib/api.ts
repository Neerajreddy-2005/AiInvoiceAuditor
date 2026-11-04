import { supabase } from "./supabase";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export function api<T>(path: string, body?: unknown): Promise<T> {
  return send<T>(path, {
    method: body !== undefined ? "POST" : "GET",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

export function uploadInvoice(file: File) {
  return send<{ invoice_id: string }>("/invoices/upload", {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream", "X-File-Name": encodeURIComponent(file.name) },
    body: file,
  });
}

async function send<T>(path: string, init: RequestInit & { headers: Record<string, string> }): Promise<T> {
  const { data } = await supabase.auth.getSession();
  const headers = { ...init.headers };
  if (data.session) headers.Authorization = `Bearer ${data.session.access_token}`;

  let response: Response;
  try {
    response = await fetch(`/api${path}`, { ...init, headers });
  } catch {
    throw new ApiError(0, "Can't reach the audit service. Check your connection and try again.");
  }
  if (response.status === 401) {
    await supabase.auth.signOut({ scope: "local" }); // this tab's session ended; leave other devices signed in
    throw new ApiError(401, "Your session has ended. Please sign in again.");
  }
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = payload?.detail;
    throw new ApiError(response.status, typeof detail === "string" ? detail : "The audit service couldn't complete that request.");
  }
  // Every route returns JSON. Anything else is a misrouted request, such as a host's fallback page.
  if (payload === null) throw new ApiError(response.status, "The audit service couldn't complete that request.");
  return payload as T;
}
