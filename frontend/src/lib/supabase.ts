import { createClient } from "@supabase/supabase-js";

declare const __SUPABASE_URL__: string;
declare const __SUPABASE_PUBLISHABLE_KEY__: string;

// Sign-in is by password only, so a link carrying tokens must never replace the session.
export const supabase = createClient(__SUPABASE_URL__, __SUPABASE_PUBLISHABLE_KEY__, {
  auth: { detectSessionInUrl: false },
});

supabase.auth.onAuthStateChange((event) => {
  if (event !== "SIGNED_OUT") return;
  try {
    localStorage.removeItem("recent-vendors"); // vendor names shouldn't outlive the session on a shared browser
  } catch {
    // Storage can be unavailable; there is nothing to clear then.
  }
});
