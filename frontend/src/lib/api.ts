import { supabase } from "./supabase";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, body?: unknown): Promise<T> {
  const { data } = await supabase.auth.getSession();
  const headers: Record<string, string> = {};
  if (data.session) headers.Authorization = `Bearer ${data.session.access_token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method: body !== undefined ? "POST" : "GET",
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
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
