import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronLeft, ChevronRight, Download, Flag, Pencil, RotateCw, X } from "lucide-react";
import { type FormEvent, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, slowApi } from "../lib/api";
import { dateTime, fieldLabel, LANGUAGES, money, plain, STATUS_LABEL, VERDICT_HELP, VERDICT_LABEL } from "../lib/format";
import type { Discrepancy, Feedback, Invoice, InvoiceDetail, Verdict } from "../lib/types";
import { useInvoices } from "./Invoices";

const HEADER_FIELDS = ["invoice_number", "invoice_date", "vendor_name", "po_number", "currency", "subtotal_amount", "tax_rate_pct", "tax_amount", "total_amount"] as const;
const LINE_FIELDS = ["item_code", "description", "quantity", "unit_price", "line_total"] as const;
const VERDICTS: Verdict[] = ["approve", "review", "reject"];
const AMOUNTS = new Set(["subtotal_amount", "tax_amount", "total_amount", "unit_price", "line_total"]);
const display = (field: string, value: number | string | null) => (AMOUNTS.has(field) ? money(value) : plain(value));

function useCorrect(id: string) {
  const queries = useQueryClient();
  return useMutation({
    mutationFn: (body: { field_name: string; line_number: number | null; corrected_value: string }) =>
      api(`/invoices/${id}/feedback`, { feedback_type: "correction", ...body }),
    onSuccess: () => queries.invalidateQueries({ queryKey: ["invoice", id] }),
  });
}

/** A figure you can click to correct. The original stays visible once corrected. */
function Editable({ id, field, line, value, shown, correction, flagged }: {
  id: string; field: string; line: number | null; value: unknown; shown: string;
  correction?: Feedback; flagged?: Discrepancy;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const correct = useCorrect(id);
  const label = fieldLabel(field, line);

  function open() {
    setDraft(correction?.corrected_value ?? (value === null || value === undefined ? "" : String(value)));
    correct.reset();
    setEditing(true);
  }
  function save(event: FormEvent) {
    event.preventDefault();
    correct.mutate({ field_name: field, line_number: line, corrected_value: draft }, { onSuccess: () => setEditing(false) });
  }

  if (editing) {
    return (
      <form className="edit" onSubmit={save}>
        <input autoFocus aria-label={`Correct ${label}`} value={draft} disabled={correct.isPending} onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Escape") setEditing(false); }} />
        <button className="icon-button" aria-label="Save correction" disabled={correct.isPending}><Check size={16} /></button>
        <button type="button" className="icon-button" aria-label="Cancel" onClick={() => setEditing(false)}><X size={16} /></button>
        {correct.error && <p className="error small" role="alert">{correct.error.message}</p>}
      </form>
    );
  }
  return (
    <button className={`figure ${flagged ? `flag-${flagged.severity}` : ""}`} onClick={open} title={`Correct ${label}`}>
      {correction ? <><s>{shown}</s> <ins>{display(field, correction.corrected_value)}</ins></> : shown}
      <Pencil size={12} className="pencil" aria-hidden="true" />
    </button>
  );
}

function Section({ title, count, children }: { title: string; count?: number; children: (open: boolean) => ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <details className="fold" onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary><h2>{title}{count !== undefined && <span className="count">{count}</span>}</h2></summary>
      <div className="fold-body">{children(open)}</div>
    </details>
  );
}

function Report({ id, runId, name }: { id: string; runId: number; name: string }) {
  const { data, isPending, error } = useQuery({
    queryKey: ["report", id, runId],
    queryFn: () => api<{ run_number: number; report_html: string | null; report_json: unknown }>(`/invoices/${id}/runs/${runId}/report`),
    staleTime: Infinity,
  });
  if (isPending) return <div className="skeleton-rows wide" aria-busy="true"><span /><span /><span /></div>;
  if (error) return <p className="error" role="alert">{error.message}</p>;
  if (!data.report_html) return <p className="muted">This run has no report.</p>;

  function download() {
    const url = URL.createObjectURL(new Blob([JSON.stringify(data!.report_json, null, 2)], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `${name}_run${data!.run_number}.json`;
    link.click();
    URL.revokeObjectURL(url);
  }
  return (
    <>
      <button className="button" onClick={download}><Download size={16} aria-hidden="true" /> Download report (JSON)</button>
      {/* Report HTML includes text read from invoices, so it gets a fully sandboxed frame. */}
      <iframe className="report" title="Validation report" sandbox="" srcDoc={data.report_html} />
    </>
  );
}

/** Invoice and reference value drawn to one scale. Skipped when either isn't a number. */
function Compare({ d }: { d: Discrepancy }) {
  const invoice = Number(d.invoice_value);
  const reference = Number(d.erp_value ?? d.expected_value);
  const usable = d.invoice_value !== null && (d.erp_value ?? d.expected_value) !== null && Number.isFinite(invoice) && Number.isFinite(reference);
  const max = Math.max(Math.abs(invoice), Math.abs(reference));
  if (!usable || max === 0) return null;
  return (
    <span className="compare" aria-hidden="true">
      <span className="invoice" style={{ width: `${(Math.abs(invoice) / max) * 100}%` }} />
      <span className="reference" style={{ width: `${(Math.abs(reference) / max) * 100}%` }} />
    </span>
  );
}

function DecisionBar({ id, runNumber, current, queue }: { id: string; runNumber?: number; current?: Feedback; queue: { done: number; total: number; next?: { invoice_id: string; label: string } } }) {
  const queries = useQueryClient();
  const navigate = useNavigate();
  const [comment, setComment] = useState("");
  const [advancing, setAdvancing] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const decide = useMutation({
    mutationFn: (decision: Verdict) => api(`/invoices/${id}/feedback`, { feedback_type: "decision", corrected_value: decision, comment: comment.trim() || null, run_number: runNumber }),
    onError: () => queries.invalidateQueries({ queryKey: ["invoice", id] }), // it may have been re-checked elsewhere
    onSuccess: () => {
      setComment("");
      queries.invalidateQueries({ queryKey: ["invoice", id] });
      queries.invalidateQueries({ queryKey: ["invoices"] });
      // A first decision moves on to the next invoice; changing an earlier one stays here.
      if (!current && queue.next) {
        setAdvancing(true);
        timer.current = window.setTimeout(() => navigate(`/invoices/${queue.next!.invoice_id}`), 1100);
      }
    },
  });
  return (
    <aside className="decision-bar" aria-label="Your decision">
      <div className="decision-inner">
        <div className="decision-state" aria-live="polite">
          {decide.error ? <span className="error">{decide.error.message}</span>
            : current ? <span><strong>Decided: {VERDICT_LABEL[current.corrected_value as Verdict]}</strong><small>{current.corrected_by}, {dateTime(current.corrected_at)}</small></span>
            : advancing ? <strong>Saved. Opening {queue.next?.label}…</strong>
            : <strong>Your decision</strong>}
        </div>
        {queue.total > 0 && (
          <div className="queue num">
            <span>{queue.done} of {queue.total} decided</span>
            <span className="progress-track"><span style={{ width: `${(queue.done / queue.total) * 100}%` }} /></span>
          </div>
        )}
        <input className="decision-note" aria-label="Comment on your decision (optional)" placeholder="Add a comment (optional)" maxLength={2000} value={comment} onChange={(e) => setComment(e.target.value)} />
        <div className="decision-buttons">
          {VERDICTS.map((v) => (
            <button key={v} className={`button decide ${v} ${current?.corrected_value === v ? "chosen" : ""}`} disabled={decide.isPending}
              aria-pressed={current?.corrected_value === v} onClick={() => decide.mutate(v)}>
              {decide.isPending && decide.variables === v ? "Saving…" : VERDICT_LABEL[v]}
            </button>
          ))}
        </div>
      </div>
    </aside>
  );
}

function Pipeline({ invoice, id }: { invoice: Invoice; id: string }) {
  const queries = useQueryClient();
  const retry = useMutation({
    mutationFn: () => api(`/invoices/${id}/retry`, {}),
    onSuccess: () => queries.invalidateQueries({ queryKey: ["invoice", id] }),
  });
  const failed = invoice.processing_status === "failed";
  return (
    <section className="page">
      <div className="notice" role={failed ? "alert" : "status"}>
        <p>{failed
          ? `Processing stopped at ${invoice.failed_stage ?? "an unknown stage"} after ${invoice.attempts} attempt(s): ${invoice.error_message}`
          : invoice.error_message ?? "This file is still being worked on. This page updates by itself."}</p>
        {failed && <button className="button primary" disabled={retry.isPending || retry.isSuccess} onClick={() => retry.mutate()}>
          <RotateCw size={16} aria-hidden="true" /> {retry.isSuccess ? "Retry started" : "Retry from where it stopped"}
        </button>}
        {retry.error && <p className="error" role="alert">{retry.error.message}</p>}
      </div>
    </section>
  );
}

export default function Review({ reviewer }: { reviewer: string }) {
  const params = useParams();
  const id = encodeURIComponent(params.id ?? ""); // a crafted link can't retarget the API path
  const navigate = useNavigate();
  const queries = useQueryClient();
  const { data: all } = useInvoices();
  const { data, isPending, error, refetch } = useQuery({
    queryKey: ["invoice", id],
    queryFn: () => api<InvoiceDetail>(`/invoices/${id}`),
    refetchInterval: (q) => (["processing", "detected"].includes(q.state.data?.invoice.processing_status ?? "") ? 4_000 : false),
  });
  const [showInfo, setShowInfo] = useState(false);
  const [highlight, setHighlight] = useState<number | null>(null);
  const linesRef = useRef<HTMLDivElement>(null);

  const position = all?.findIndex((r) => r.invoice_id === id) ?? -1;
  const previous = position > 0 ? all![position - 1] : undefined;
  const next = all && position >= 0 ? all[position + 1] : undefined;

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (/INPUT|SELECT|TEXTAREA/.test((event.target as HTMLElement).tagName) || event.metaKey || event.ctrlKey) return;
      if (event.key === "j" && next) navigate(`/invoices/${next.invoice_id}`);
      if (event.key === "k" && previous) navigate(`/invoices/${previous.invoice_id}`);
    }
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [next, previous, navigate]);

  useEffect(() => { scrollTo(0, 0); setHighlight(null); }, [id]);

  const revalidate = useMutation({
    mutationFn: () => slowApi<{ run_number: number; recommendation: Verdict }>(`/invoices/${id}/revalidate`, {}),
    onSuccess: () => {
      queries.invalidateQueries({ queryKey: ["invoice", id] });
      queries.invalidateQueries({ queryKey: ["invoices"] });
    },
  });

  const derived = useMemo(() => {
    const feedback = data?.feedback ?? [];
    const corrections = new Map<string, Feedback>();
    for (const f of feedback) {
      const key = `${f.line_number ?? "h"}:${f.field_name}`;
      if (f.feedback_type === "correction" && !corrections.has(key)) corrections.set(key, f);
    }
    // A decision made before the latest re-check no longer stands, matching the API's "decided".
    const checkedAt = data?.runs.find((r) => r.completed_at)?.completed_at;
    return {
      corrections,
      decision: feedback.find((f) => f.feedback_type === "decision" && (!checkedAt || new Date(f.corrected_at) >= new Date(checkedAt))),
      pending: feedback.filter((f) => f.feedback_type === "correction" && !f.revalidated).length,
    };
  }, [data]);

  if (isPending) return <div className="skeleton-page" aria-busy="true" />;
  if (error) {
    return (
      <section className="page"><div className="notice error-notice" role="alert">
        <p>{error.message}</p>
        <button className="button" onClick={() => refetch()}>Try again</button> <Link className="button quiet" to="/">Back to invoices</Link>
      </div></section>
    );
  }

  const { invoice: inv, line_items: lines, runs, latest_run_id: latestId, discrepancies, feedback } = data;
  const latest = runs.find((r) => r.run_id === latestId);
  const verdict = inv.recommendation;
  const title = inv.invoice_number ?? inv.file_name;
  const reasons = latest?.recommendation_reasons ?? [];
  const serious = discrepancies.filter((d) => d.severity !== "low");
  const visible = showInfo ? discrepancies : serious;
  const hiddenInfo = discrepancies.length - serious.length;
  const flagFor = (field: string, line: number | null) => serious.find((d) => d.field_name === field && (d.line_number ?? null) === line);
  const lineFlagged = (n: number) => serious.some((d) => d.line_number === n);
  const completed = inv.processing_status === "completed";
  const flagged = (all ?? []).filter((r) => r.processing_status === "completed" && r.recommendation !== "approve");
  const later = [...flagged.slice(Math.max(flagged.findIndex((r) => r.invoice_id === id), 0) + 1), ...flagged];
  const nextOpen = later.find((r) => !r.decided && r.invoice_id !== id);
  const queue = {
    done: flagged.filter((r) => r.decided).length,
    total: flagged.length,
    next: nextOpen && { invoice_id: nextOpen.invoice_id, label: nextOpen.invoice_number ?? nextOpen.file_name },
  };
  const language = inv.source_language ? LANGUAGES[inv.source_language] ?? inv.source_language : "unknown";

  function jumpTo(line: number | null) {
    if (line === null) return;
    setHighlight(line);
    linesRef.current?.querySelector(`[data-line="${line}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  return (
    <>
      <section className={`field verdict-field ${completed && verdict ? verdict : "neutral"}`}>
        <div className="field-inner">
          <div className="field-top">
            <Link to="/" className="back"><ChevronLeft size={18} aria-hidden="true" /> Invoices</Link>
            <div className="pager">
              {previous ? <Link className="icon-button" to={`/invoices/${previous.invoice_id}`} aria-label="Previous invoice"><ChevronLeft size={18} /></Link> : <span className="icon-button off"><ChevronLeft size={18} /></span>}
              {next ? <Link className="icon-button" to={`/invoices/${next.invoice_id}`} aria-label="Next invoice"><ChevronRight size={18} /></Link> : <span className="icon-button off"><ChevronRight size={18} /></span>}
            </div>
          </div>
          <div className="verdict-grid">
            <div>
              <p className="verdict-sub">{title} · {inv.vendor_name ?? "Vendor not read"}</p>
              <h1 className="verdict-word">{completed && verdict ? VERDICT_LABEL[verdict] : STATUS_LABEL[inv.processing_status]}</h1>
              <p className="verdict-why">{completed && verdict ? (reasons[0] ?? VERDICT_HELP[verdict]) : inv.file_name}</p>
              {reasons.length > 1 && <ul className="verdict-more">{reasons.slice(1).map((r) => <li key={r}>{r}</li>)}</ul>}
            </div>
            <div className="verdict-total">
              <span>Total</span>
              <strong>{money(inv.total_amount, inv.currency)}</strong>
              <span className="verdict-sys">System recommendation, run {latest?.run_number ?? "—"}</span>
            </div>
          </div>
        </div>
      </section>

      {!completed ? <Pipeline invoice={inv} id={id} /> : (
        <section className="page review-page">
          {derived.pending > 0 ? (
            <div className="pending" role="status">
              <p><strong>{derived.pending} correction{derived.pending === 1 ? "" : "s"} not re-checked yet.</strong> The recommendation above doesn't include {derived.pending === 1 ? "it" : "them"}.</p>
              <button className="button primary" disabled={revalidate.isPending} onClick={() => revalidate.mutate()}>
                <RotateCw size={16} aria-hidden="true" className={revalidate.isPending ? "spin" : ""} /> {revalidate.isPending ? "Re-checking against the ERP…" : "Re-check now"}
              </button>
            </div>
          ) : (
            // Rules and the ERP change too, so a re-check is useful even with no corrections waiting.
            <div className="recheck">
              <p className="muted small">Checks this invoice again with the current rules and ERP data.{derived.decision ? " This reopens your decision." : ""}</p>
              <button className="button" disabled={revalidate.isPending} onClick={() => revalidate.mutate()}>
                <RotateCw size={16} aria-hidden="true" className={revalidate.isPending ? "spin" : ""} /> {revalidate.isPending ? "Re-checking against the ERP…" : "Re-check"}
              </button>
            </div>
          )}
          {revalidate.error && <p className="error" role="alert">{revalidate.error.message}</p>}

          {latest?.summary && <p className="lede">{latest.summary}</p>}

          <div className="columns">
            <section aria-labelledby="evidence">
              <header className="section-head">
                <h2 id="evidence">What the checks found</h2>
                {hiddenInfo > 0 && (
                  <label className="toggle"><input type="checkbox" checked={showInfo} onChange={(e) => setShowInfo(e.target.checked)} /> Show {hiddenInfo} informational</label>
                )}
              </header>
              {visible.length === 0 ? <p className="clear"><Check size={18} aria-hidden="true" /> No problems found.</p> : (
                <ol className="evidence">
                  {visible.map((d) => (
                    <li key={d.id} className={`sev-${d.severity}`}>
                      <button onClick={() => jumpTo(d.line_number)} disabled={d.line_number === null}>
                        <span className="evidence-head">
                          <Flag size={14} aria-hidden="true" /> <strong>{fieldLabel(d.field_name, d.line_number)}</strong>
                          {d.deviation_pct !== null && <span className="num dev">{Number(d.deviation_pct) > 0 ? "+" : ""}{Number(d.deviation_pct).toFixed(1)}%</span>}
                        </span>
                        <span className="evidence-msg">{d.message}</span>
                        {(d.invoice_value ?? d.erp_value ?? d.expected_value) !== null && <span className="triple">
                          <span><small>Invoice</small><b className="num">{plain(d.invoice_value)}</b></span>
                          <span><small>ERP</small><b className="num">{plain(d.erp_value)}</b></span>
                          <span><small>Expected</small><b className="num">{plain(d.expected_value)}</b></span>
                        </span>}
                        <Compare d={d} />
                      </button>
                    </li>
                  ))}
                </ol>
              )}
            </section>

            <section aria-labelledby="details">
              <header className="section-head"><h2 id="details">Invoice details</h2><span className="muted small">Click a value to correct it</span></header>
              <dl className="details">
                {HEADER_FIELDS.map((f) => {
                  const value = inv[f];
                  return (
                    <div key={f}>
                      <dt>{fieldLabel(f)}</dt>
                      <dd><Editable id={id} field={f} line={null} value={value} shown={value === null ? "Missing" : display(f, value)}
                        correction={derived.corrections.get(`h:${f}`)} flagged={flagFor(f, null)} /></dd>
                    </div>
                  );
                })}
              </dl>
              <p className="muted small read-note">Read by {inv.extraction_method ?? "unknown method"} · {language}
                {inv.translation_confidence !== null && ` · translation confidence ${Math.round(Number(inv.translation_confidence) * 100)}%`}</p>
            </section>
          </div>

          <section aria-labelledby="lines">
            <header className="section-head"><h2 id="lines">Line items</h2><span className="muted small">{lines.length} {lines.length === 1 ? "line" : "lines"}</span></header>
            {lines.length === 0 ? <p className="muted">This invoice has no line items.</p> : (
              <div className="lines" ref={linesRef} role="table" aria-label="Line items">
                <div className="line head" role="row">
                  {["", "Line", "Item", "Description", "Quantity", "Unit price", "Line total"].map((h, i) => <span key={i} role="columnheader">{h}</span>)}
                </div>
                {lines.map((li) => {
                  const bad = lineFlagged(li.line_number);
                  return (
                    <div key={li.line_number} data-line={li.line_number} role="row" className={`line ${highlight === li.line_number ? "lit" : ""}`}>
                      <span role="cell" className={`mark ${bad ? "bad" : "good"}`} aria-label={bad ? "Has a problem" : "No problem found"}>
                        {bad ? <X size={16} /> : <Check size={16} />}
                      </span>
                      <span role="cell" className="num">{li.line_number}</span>
                      {LINE_FIELDS.map((f) => (
                        <span role="cell" key={f} data-label={fieldLabel(f)} className={["quantity", "unit_price", "line_total"].includes(f) ? "num" : ""}>
                          <Editable id={id} field={f} line={li.line_number} value={li[f]} shown={display(f, li[f])}
                            correction={derived.corrections.get(`${li.line_number}:${f}`)} flagged={flagFor(f, li.line_number)} />
                        </span>
                      ))}
                    </div>
                  );
                })}
              </div>
            )}
          </section>

          <div className="folds">
            {latestId !== null && <Section title="Full report">{(open) => open && <Report id={id} runId={latestId} name={inv.invoice_number ?? id} />}</Section>}
            <Section title="Document text">{() => (
              <div className="texts">
                <div><h3>As read from the file</h3><pre>{inv.raw_extracted_text ?? "No text was read."}</pre></div>
                <div><h3>English translation</h3><pre>{inv.translated_text ?? "No translation needed."}</pre></div>
              </div>
            )}</Section>
            <Section title="History" count={runs.length + feedback.length}>{() => (
              <>
                <table className="plain">
                  <caption>Validation runs</caption>
                  <thead><tr><th>Run</th><th>Trigger</th><th>By</th><th>Result</th><th>Completed</th></tr></thead>
                  <tbody>{runs.map((r) => <tr key={r.run_id}><td className="num">{r.run_number}</td><td>{r.trigger}</td><td>{r.triggered_by ?? "System"}</td><td>{r.recommendation ? VERDICT_LABEL[r.recommendation] : "—"}</td><td>{dateTime(r.completed_at)}</td></tr>)}</tbody>
                </table>
                {feedback.length === 0 ? <p className="muted">No corrections or decisions yet. Anything you change is recorded under {reviewer}.</p> : (
                  <div className="scroll-x wide">
                    <table className="plain">
                      <caption>Changes and decisions</caption>
                      <thead><tr><th>When</th><th>Who</th><th>What</th><th>Original</th><th>New</th><th>Comment</th><th>Re-checked</th></tr></thead>
                      <tbody>{feedback.map((f) => (
                        <tr key={f.id}>
                          <td className="nowrap">{dateTime(f.corrected_at)}</td><td>{f.corrected_by}</td>
                          <td>{f.feedback_type === "decision" ? "Decision" : f.feedback_type === "comment" ? "Note" : fieldLabel(f.field_name, f.line_number)}</td>
                          <td>{f.original_value ?? "—"}</td><td>{f.corrected_value ?? "—"}</td><td>{f.comment ?? ""}</td>
                          <td>{f.feedback_type === "correction" ? (f.revalidated ? "Yes" : "Not yet") : ""}</td>
                        </tr>
                      ))}</tbody>
                    </table>
                  </div>
                )}
              </>
            )}</Section>
          </div>
        </section>
      )}
      {completed && <DecisionBar key={id} id={id} runNumber={latest?.run_number} current={derived.decision} queue={queue} />}
    </>
  );
}
