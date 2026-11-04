import { useQuery } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { CountUp } from "../components/CountUp";
import { VendorPicker } from "../components/VendorPicker";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { fieldLabel, money, shortDate, STATUS_LABEL, VERDICT_LABEL } from "../lib/format";
import type { InvoiceDetail, InvoiceRow, Verdict } from "../lib/types";

const ORDER: Verdict[] = ["reject", "review", "approve"];

export const useInvoices = () =>
  useQuery({
    queryKey: ["invoices"],
    queryFn: () => api<InvoiceRow[]>("/invoices"),
    refetchInterval: (query) =>
      query.state.data?.some((r) => r.processing_status === "processing" || r.processing_status === "detected") ? 4_000 : 15_000,
  });

function VerdictMark({ row }: { row: InvoiceRow }) {
  if (row.recommendation && row.processing_status === "completed") {
    return <span className={`verdict-tag ${row.recommendation}`}>{VERDICT_LABEL[row.recommendation]}</span>;
  }
  return <span className={`status-tag ${row.processing_status}`}>{STATUS_LABEL[row.processing_status]}</span>;
}

/** Problems on an invoice, shown on hover. Also loads the page the auditor is likely to open next. */
function Peek({ row, top }: { row: InvoiceRow; top: number }) {
  const { data } = useQuery({ queryKey: ["invoice", row.invoice_id], queryFn: () => api<InvoiceDetail>(`/invoices/${row.invoice_id}`) });
  const problems = data?.discrepancies.filter((d) => d.severity !== "low") ?? [];
  return (
    <aside className="peek" style={{ top: Math.max(72, Math.min(top, innerHeight - 260)) }} aria-hidden="true">
      <h3>{row.invoice_number ?? row.file_name}</h3>
      {!data ? <p>Loading problems…</p> : problems.length === 0 ? <p>No problems found.</p> : (
        <ul>
          {problems.slice(0, 4).map((d) => (
            <li key={d.id} className={d.severity}><i /><span><strong>{fieldLabel(d.field_name, d.line_number)}</strong><br />{d.message}</span></li>
          ))}
        </ul>
      )}
      {problems.length > 4 && <p>And {problems.length - 4} more</p>}
    </aside>
  );
}

export function Invoices() {
  const { data: rows, isPending, error, refetch } = useInvoices();
  const [verdict, setVerdict] = useState<Verdict | null>(null);
  const [vendor, setVendor] = useState("");
  const [search, setSearch] = useState("");
  const [cursor, setCursor] = useState(0);
  const [peek, setPeek] = useState<{ row: InvoiceRow; top: number } | null>(null);
  const peekTimer = useRef<number | undefined>(undefined);
  const searchBox = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();

  const counts = useMemo(() => {
    const done = (rows ?? []).filter((r) => r.processing_status === "completed");
    return Object.fromEntries(ORDER.map((v) => [v, done.filter((r) => r.recommendation === v).length])) as Record<Verdict, number>;
  }, [rows]);
  const shown = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return (rows ?? []).filter((r) =>
      (!verdict || r.recommendation === verdict) &&
      (!vendor || r.vendor_name === vendor) &&
      (!needle || `${r.invoice_number} ${r.file_name} ${r.vendor_name}`.toLowerCase().includes(needle)));
  }, [rows, verdict, vendor, search]);
  const needsAttention = (rows ?? []).filter((r) => r.processing_status === "completed" && r.recommendation !== "approve" && !r.decided).length;
  const filtered = Boolean(verdict || vendor || search);

  const summary = useMemo(() => {
    const flagged = (rows ?? []).filter((r) => r.processing_status === "completed" && r.recommendation !== "approve");
    const held = new Map<string, number>();
    for (const r of flagged) {
      if (!r.decided && r.currency) held.set(r.currency, (held.get(r.currency) ?? 0) + Number(r.total_amount ?? 0));
    }
    const perVendor = new Map<string, Record<Verdict, number>>();
    for (const r of rows ?? []) {
      if (!r.vendor_name || !r.recommendation) continue;
      const entry = perVendor.get(r.vendor_name) ?? { reject: 0, review: 0, approve: 0 };
      entry[r.recommendation] += 1;
      perVendor.set(r.vendor_name, entry);
    }
    const vendorRows = [...perVendor.entries()]
      .map(([name, c]) => ({ name, ...c, total: c.reject + c.review + c.approve }))
      .sort((a, b) => b.reject + b.review - (a.reject + a.review) || a.name.localeCompare(b.name));
    // Amounts in an unknown currency can't be added up, so these are only counted.
    const noCurrency = flagged.filter((r) => !r.decided && !r.currency).length;
    return { held: [...held.entries()].sort(), noCurrency, total: flagged.length, decided: flagged.filter((r) => r.decided).length, vendorRows };
  }, [rows]);

  useEffect(() => setCursor(0), [verdict, vendor, search]);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      const target = event.target as HTMLElement;
      if (/INPUT|SELECT|TEXTAREA/.test(target.tagName) || event.metaKey || event.ctrlKey) return;
      if (event.key === "/") { event.preventDefault(); searchBox.current?.focus(); }
      if (event.key === "j") setCursor((c) => Math.min(c + 1, shown.length - 1));
      if (event.key === "k") setCursor((c) => Math.max(c - 1, 0));
      if (event.key === "Enter" && target.tagName === "BODY" && shown[cursor]) navigate(`/invoices/${shown[cursor].invoice_id}`);
    }
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [shown, cursor, navigate]);

  return (
    <>
      <section className="field brand">
        <div className="field-inner">
          <div className="field-head">
          <h1 className="display">
            {isPending ? "Loading invoices" : needsAttention > 0
              ? `${needsAttention} ${needsAttention === 1 ? "invoice needs" : "invoices need"} a decision`
              : rows?.length ? "Nothing is waiting on you" : "No invoices yet"}
          </h1>
          {summary.total > 0 && (
            <div className="field-meta">
              {(summary.held.length > 0 || summary.noCurrency > 0) && (
                <div>
                  <span className="meta-label">Held until decided</span>
                  <div className="risk-values num">
                    {summary.held.map(([currency, amount]) => (
                      <span key={currency}><CountUp value={amount} format={(n) => money(n, currency)} /></span>
                    ))}
                    {summary.noCurrency > 0 && <span>{summary.noCurrency} with no currency</span>}
                  </div>
                </div>
              )}
              <div className="progress-block">
                <span className="meta-label">Your progress</span>
                <strong className="num">{summary.decided} of {summary.total} decided</strong>
                <span className="progress-track"><span style={{ width: `${(summary.decided / summary.total) * 100}%` }} /></span>
              </div>
            </div>
          )}
          </div>
          <div className="strip" role="group" aria-label="Filter by recommendation">
            {ORDER.map((v) => (
              <button key={v} className={`strip-part ${v} ${verdict && verdict !== v ? "dim" : ""}`}
                style={{ flexGrow: Math.max(counts[v], rows?.length ? 0.6 : 1) }}
                aria-pressed={verdict === v} onClick={() => setVerdict(verdict === v ? null : v)}>
                <strong><CountUp value={counts[v]} /></strong> <span>{VERDICT_LABEL[v]}</span>
              </button>
            ))}
          </div>
        </div>
      </section>

      <section className="page">
        <div className="filters">
          <label className="search">
            <Search size={18} aria-hidden="true" />
            <span className="sr-only">Search invoices</span>
            <input ref={searchBox} type="search" placeholder="Search number, vendor or file  ( / )" value={search} onChange={(e) => setSearch(e.target.value)} />
          </label>
          <VendorPicker vendors={summary.vendorRows} value={vendor} onChange={setVendor} />
          {filtered && <button className="button quiet" onClick={() => { setVerdict(null); setVendor(""); setSearch(""); }}>Clear filters</button>}
          <span className="muted small push">{shown.length} of {rows?.length ?? 0} · <kbd>J</kbd> <kbd>K</kbd> to move, <kbd>Enter</kbd> to open</span>
        </div>

        {error && (
          <div className="notice error-notice" role="alert">
            <p>{error.message}</p>
            <button className="button" onClick={() => refetch()}>Try again</button>
          </div>
        )}
        {isPending && <div className="skeleton-rows" aria-busy="true">{Array.from({ length: 6 }, (_, i) => <span key={i} />)}</div>}
        {rows?.length === 0 && (
          <div className="empty">
            <h2>Waiting for the first invoice</h2>
            <p>Add PDF, DOCX or PNG files to the incoming folder. They're picked up within seconds and appear here as they're checked.</p>
          </div>
        )}
        {rows && rows.length > 0 && shown.length === 0 && (
          <div className="empty"><h2>No invoices match those filters</h2><p>Clear a filter or try a different search.</p></div>
        )}
        {shown.length > 0 && (
          <div className="invoice-table" role="table" aria-label="Invoices">
            <div className="row head" role="row">
              {["Recommendation", "Invoice", "Why", "Date", "Total"].map((h) => <span key={h} role="columnheader">{h}</span>)}
            </div>
            {shown.map((row, index) => (
              <a key={row.invoice_id} role="row" data-verdict={row.processing_status === "completed" ? row.recommendation ?? undefined : undefined} className={`row ${index === cursor ? "cursor" : ""}`}
                href={`/invoices/${row.invoice_id}`}
                onClick={(e) => { if (!e.metaKey && !e.ctrlKey) { e.preventDefault(); navigate(`/invoices/${row.invoice_id}`); } }}
                onMouseEnter={(e) => {
                  setCursor(index);
                  const top = e.currentTarget.getBoundingClientRect().bottom + 6;
                  clearTimeout(peekTimer.current);
                  peekTimer.current = window.setTimeout(() => setPeek(row.processing_status === "completed" && row.recommendation !== "approve" ? { row, top } : null), 220);
                }}
                onMouseLeave={() => { clearTimeout(peekTimer.current); setPeek(null); }}>
                <span role="cell"><VerdictMark row={row} /></span>
                <span role="cell" className="identity">
                  <strong>{row.invoice_number ?? row.file_name}</strong>
                  {row.decision && (
                    <span className={`decided ${row.decision}`}>
                      {row.recommendation && row.recommendation !== row.decision ? `${VERDICT_LABEL[row.recommendation]} → ` : ""}Decided: {VERDICT_LABEL[row.decision]}
                    </span>
                  )}
                  <small>{row.vendor_name ?? "Vendor not read"}</small>
                </span>
                <span role="cell" className="why">{row.top_reason ?? row.error_message ?? "—"}</span>
                <span role="cell" className="num date">{shortDate(row.invoice_date)}</span>
                <span role="cell" className="num total">{money(row.total_amount, row.currency)}</span>
              </a>
            ))}
          </div>
        )}
        {peek && <Peek row={peek.row} top={peek.top} />}
      </section>
    </>
  );
}
