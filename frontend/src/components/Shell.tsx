import { LogOut, Moon, Sun } from "lucide-react";
import { type MouseEvent, type ReactNode, useState } from "react";
import { flushSync } from "react-dom";
import { Link, NavLink, useLocation } from "react-router-dom";
import { supabase } from "../lib/supabase";

function ThemeSwitch() {
  const [theme, setTheme] = useState(document.documentElement.dataset.theme ?? "light");

  function apply(next: string) {
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("theme", next);
    } catch {
      // Private windows can refuse storage; the theme still applies for this visit.
    }
    setTheme(next);
  }

  function toggle(event: MouseEvent<HTMLButtonElement>) {
    const next = theme === "dark" ? "light" : "dark";
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!document.startViewTransition || reduced) return apply(next);
    const { clientX: x, clientY: y } = event;
    const radius = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
    document.startViewTransition(() => flushSync(() => apply(next))).ready.then(() => {
      document.documentElement.animate(
        { clipPath: [`circle(0 at ${x}px ${y}px)`, `circle(${radius}px at ${x}px ${y}px)`] },
        { duration: 420, easing: "cubic-bezier(0.16, 1, 0.3, 1)", pseudoElement: "::view-transition-new(root)" },
      );
    });
  }

  const label = theme === "dark" ? "Switch to light theme" : "Switch to dark theme";
  return (
    <button className="icon-button" onClick={toggle} aria-label={label} title={label}>
      {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
    </button>
  );
}

export function Shell({ email, children }: { email: string; children: ReactNode }) {
  const { pathname } = useLocation();
  return (
    <>
      <header className="topbar">
        <Link to="/" className="wordmark">Invoice Auditor</Link>
        <nav aria-label="Main">
          <NavLink to="/" end>Invoices</NavLink>
          <NavLink to="/ask">Ask</NavLink>
          <NavLink to="/log">Processing log</NavLink>
        </nav>
        <div className="topbar-end">
          <span className="who" title={email}>{email}</span>
          <ThemeSwitch />
          <button className="icon-button" onClick={() => supabase.auth.signOut()} aria-label="Sign out" title="Sign out">
            <LogOut size={18} />
          </button>
        </div>
      </header>
      <main key={pathname} className="enter">{children}</main>
    </>
  );
}
