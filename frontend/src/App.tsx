import type { Session } from "@supabase/supabase-js";
import { useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { Shell } from "./components/Shell";
import { supabase } from "./lib/supabase";
import { Invoices } from "./pages/Invoices";
import { SignIn } from "./pages/SignIn";

const Review = lazy(() => import("./pages/Review"));
const Ask = lazy(() => import("./pages/Ask"));
const Log = lazy(() => import("./pages/Log"));

export function App() {
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const queries = useQueryClient();

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => setSession(data.session));
    const { data } = supabase.auth.onAuthStateChange((event, next) => {
      setSession(next);
      if (event === "SIGNED_OUT") queries.clear();
    });
    return () => data.subscription.unsubscribe();
  }, [queries]);

  if (session === undefined) return null;
  if (session === null) return <SignIn />;

  return (
    <Shell email={session.user.email ?? ""}>
      <Suspense fallback={<div className="skeleton-page" aria-busy="true" />}>
        <Routes>
          <Route path="/" element={<Invoices />} />
          <Route path="/invoices/:id" element={<Review reviewer={session.user.email ?? ""} />} />
          <Route path="/ask" element={<Ask />} />
          <Route path="/log" element={<Log />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Suspense>
    </Shell>
  );
}
