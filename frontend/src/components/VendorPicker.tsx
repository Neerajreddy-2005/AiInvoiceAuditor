import { ChevronDown, Search } from "lucide-react";
import { type KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";

export interface VendorSummary {
  name: string;
  reject: number;
  review: number;
  approve: number;
  total: number;
}

const RECENT_KEY = "recent-vendors";
const RECENT_LIMIT = 5;
const fold = (text: string) => text.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();

function readRecent(): string[] {
  try {
    const saved = JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]");
    return Array.isArray(saved) ? saved.filter((v) => typeof v === "string") : [];
  } catch {
    return [];
  }
}

function saveRecent(names: string[]) {
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(names));
  } catch {
    // Private windows can refuse storage; the menu just has no recent section.
  }
}

/** Vendor filter that stays usable with hundreds of vendors: search, recent picks, then all by most flagged. */
export function VendorPicker({ vendors, value, onChange }: { vendors: VendorSummary[]; value: string; onChange: (name: string) => void }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [recent, setRecent] = useState<string[]>(readRecent);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLUListElement>(null);

  const { recentRows, otherRows } = useMemo(() => {
    const needle = fold(query.trim());
    if (needle) return { recentRows: [], otherRows: vendors.filter((v) => fold(v.name).includes(needle)) };
    const byName = new Map(vendors.map((v) => [v.name, v]));
    return { recentRows: recent.flatMap((name) => byName.get(name) ?? []), otherRows: vendors };
  }, [vendors, recent, query]);

  const showAll = !query.trim();
  const choices = [...(showAll ? [""] : []), ...recentRows.map((v) => v.name), ...otherRows.map((v) => v.name)];

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    addEventListener("pointerdown", onPointer);
    return () => removeEventListener("pointerdown", onPointer);
  }, [open]);

  useEffect(() => setActive(0), [query]);

  useEffect(() => {
    if (open) list.current?.querySelector('[data-active="true"]')?.scrollIntoView({ block: "nearest" });
  }, [active, open]);

  function close() {
    setOpen(false);
    setQuery("");
    trigger.current?.focus();
  }

  function choose(name: string) {
    onChange(name);
    if (name) {
      const next = [name, ...recent.filter((v) => v !== name)].slice(0, RECENT_LIMIT);
      setRecent(next);
      saveRecent(next);
    }
    close();
  }

  function onKey(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown") { event.preventDefault(); setActive((i) => Math.min(i + 1, choices.length - 1)); }
    if (event.key === "ArrowUp") { event.preventDefault(); setActive((i) => Math.max(i - 1, 0)); }
    if (event.key === "Enter") { event.preventDefault(); if (choices[active] !== undefined) choose(choices[active]); }
    if (event.key === "Escape") { event.preventDefault(); close(); }
  }

  let index = showAll ? 0 : -1;
  const row = (v: VendorSummary, group: string) => {
    index += 1;
    const position = index;
    return (
      <li key={`${group}-${v.name}`} id={`vendor-${position}`} role="option" aria-selected={value === v.name} data-active={active === position}
        className="picker-option" onPointerEnter={() => setActive(position)} onClick={() => choose(v.name)}>
        <span className="picker-line"><span className="picker-name" title={v.name}>{v.name}</span><span className="num picker-count">{v.reject + v.review} of {v.total} flagged</span></span>
        <span className="vendor-bar" aria-hidden="true">
          {(["reject", "review", "approve"] as const).map((k) => v[k] > 0 && <span key={k} className={k} style={{ flexGrow: v[k] }} />)}
        </span>
      </li>
    );
  };

  return (
    <div className="picker" ref={root}>
      <button ref={trigger} type="button" className="picker-trigger" aria-haspopup="listbox" aria-expanded={open} aria-label={`Vendor: ${value || "All vendors"}`}
        onClick={() => (open ? close() : setOpen(true))}>
        <span className="picker-value">{value || "All vendors"}</span>
        <ChevronDown size={18} aria-hidden="true" />
      </button>
      {open && (
        <div className="picker-panel">
          <label className="picker-search">
            <Search size={16} aria-hidden="true" />
            <span className="sr-only">Search {vendors.length} vendors</span>
            <input autoFocus role="combobox" aria-expanded="true" aria-controls="vendor-list" aria-activedescendant={`vendor-${active}`}
              placeholder={`Search ${vendors.length} vendors`} value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={onKey} />
          </label>
          <ul id="vendor-list" ref={list} role="listbox" aria-label="Vendors">
            {showAll && (
              <li id="vendor-0" role="option" aria-selected={!value} data-active={active === 0} className="picker-option"
                onPointerEnter={() => setActive(0)} onClick={() => choose("")}>
                <span className="picker-name">All vendors</span>
              </li>
            )}
            {recentRows.length > 0 && <li role="presentation" className="picker-group">Recent</li>}
            {recentRows.map((v) => row(v, "recent"))}
            {showAll && <li role="presentation" className="picker-group">Most flagged first</li>}
            {otherRows.map((v) => row(v, "all"))}
            {choices.length === 0 && <li role="presentation" className="picker-empty">No vendor matches that search.</li>}
          </ul>
        </div>
      )}
    </div>
  );
}
