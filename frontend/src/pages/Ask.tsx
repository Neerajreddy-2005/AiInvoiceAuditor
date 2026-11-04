import { useMutation } from "@tanstack/react-query";
import { ArrowUp } from "lucide-react";
import { type FormEvent, Fragment, useState } from "react";
import { flushSync } from "react-dom";
import { Link } from "react-router-dom";
import { slowApi } from "../lib/api";
import type { Answer, Source } from "../lib/types";
import { useInvoices } from "./Invoices";

const EXAMPLES = ["Why was INV-1001 flagged?", "How many invoices were rejected?", "What did HafenLogistik invoice?", "Total invoiced per currency"];
const SCORES: [string, string][] = [
  ["answer_relevance", "Answers the question"], ["groundedness", "Supported by sources"], ["context_relevance", "Relevant sources"], ["average", "Average (needs over 0.70)"],
];

type Message = { id: number; question: string; answer?: Answer; error?: string };

type Cite = { id: number; onOpen: () => void };

function Inline({ text, cite }: { text: string; cite: Cite }) {
  // Only bold, `names` and citations like [2], [1, 2] or [1], [2] get markup. Everything else stays plain text.
  // A bracket straight after a word is an index, as in items[1], not a citation.
  return <>{text.split(/(\*\*[^*]+\*\*|`[^`]+`|(?<![\w\]])\[\d+(?:,\s*\d+)*\](?:,\s*\[\d+(?:,\s*\d+)*\])*)/).map((part, i) => {
    if (i % 2 === 0) return <Fragment key={i}>{part}</Fragment>; // split() puts the matches at odd positions
    if (part.startsWith("**")) return <strong key={i}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`")) return <code key={i}>{part.slice(1, -1)}</code>;
    // The sources must be open before the browser follows the link, or it has nowhere to go.
    return (part.match(/\d+/g) ?? []).map((ref) => (
      <a key={`${i}-${ref}`} className="cite" href={`#src-${cite.id}-${ref}`} aria-label={`Source ${ref}`}
        onClick={() => flushSync(cite.onOpen)}>{ref}</a>
    ));
  })}</>;
}

function AnswerText({ text, cite }: { text: string; cite: Cite }) {
  const blocks: (string | string[])[] = [];
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    const bullet = line.match(/^[*-]\s+(.*)$/)?.[1];
    const last = blocks[blocks.length - 1];
    if (bullet && Array.isArray(last)) last.push(bullet);
    else if (bullet) blocks.push([bullet]);
    else if (line) blocks.push(line);
  }
  return (
    <div className="answer-text">
      {blocks.map((block, i) => Array.isArray(block)
        ? <ul key={i}>{block.map((item, j) => <li key={j}><Inline text={item} cite={cite} /></li>)}</ul>
        : <p key={i}><Inline text={block} cite={cite} /></p>)}
    </div>
  );
}

function AnswerTable({ rows }: { rows: NonNullable<Answer["rows"]> }) {
  const columns = Object.keys(rows[0]);
  return (
    <div className="answer-table">
      <table className="plain">
        <thead><tr>{columns.map((c) => <th key={c} scope="col">{c.replace(/_/g, " ")}</th>)}</tr></thead>
        <tbody>{rows.map((row, i) => <tr key={i}>{columns.map((c) => <td key={c}>{row[c] ?? "—"}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

const SOURCE_TYPES: Record<string, string> = { report: "Audit report", invoice: "Invoice text" };

/** Report text as table rows, one per part: "Recommendation", "Reasons" and so on. */
function ReportRows({ text }: { text: string }) {
  const lines = text.split(/\n+|(?<=[.)…])\s+(?=(?:Summary|Recommendation|Reasons|Discrepancy|Auditor correction)\b)/);
  return (
    <table className="plain source-details">
      <tbody>
        {lines.filter((line) => line.trim()).map((line, i) => {
          const found = line.match(/^(Summary|Recommendation|Reasons|Auditor correction|Discrepancy)(?: \((\w+), \w+\))?:\s*(.*)$/s);
          const label = found ? `${found[1]}${found[2] ? ` (${found[2]})` : ""}` : "Overview";
          return <tr key={i}><th scope="row">{label}</th><td>{found ? found[3] : line}</td></tr>;
        })}
      </tbody>
    </table>
  );
}

function SourceCard({ source, id }: { source: Source; id: number }) {
  const facts = ([["Vendor", source.vendor], ["Purchase order", source.po_number], ["File", source.file_name]] as const)
    .filter(([, value]) => value);
  return (
    <li id={`src-${id}-${source.ref}`}>
      <div className="source-head">
        <span className="cite">{source.ref}</span>
        <strong>{source.invoice_number ?? "No number"}</strong>
        <span className="source-type">{SOURCE_TYPES[source.source_type] ?? source.source_type}</span>
        {source.invoice_id && <Link className="source-open" to={`/invoices/${source.invoice_id}`}>Open invoice</Link>}
      </div>
      {facts.length > 0 && (
        <table className="plain source-facts">
          <thead><tr>{facts.map(([label]) => <th key={label} scope="col">{label}</th>)}</tr></thead>
          <tbody><tr>{facts.map(([label, value]) => <td key={label}>{value}</td>)}</tr></tbody>
        </table>
      )}
      {source.source_type === "report" ? <ReportRows text={source.excerpt} /> : <p className="source-plain">{source.excerpt}</p>}
    </li>
  );
}

function AnswerBlock({ message }: { message: Message }) {
  const { answer, id } = message;
  const [sourcesOpen, setSourcesOpen] = useState(false);
  if (!answer) return null;
  const scores = answer.scores;
  const rows = answer.rows ?? [];
  const isTable = rows.length > 1 || (rows.length === 1 && Object.keys(rows[0]).length > 1);
  const sources = answer.sources ?? [];
  const cited = new Set([...answer.answer.matchAll(/(?<!\w)\[(\d+(?:,\s*\d+)*)\]/g)].flatMap((m) => m[1].split(/,\s*/).map(Number)));
  // Passages the answer never cites were only searched, so they stay out of the way.
  const used = cited.size ? sources.filter((s) => cited.has(s.ref)) : sources;
  const searched = cited.size ? sources.filter((s) => !cited.has(s.ref)) : [];
  return (
    <div className="answer">
      {isTable ? <AnswerTable rows={rows} /> : <AnswerText text={answer.answer} cite={{ id, onOpen: () => setSourcesOpen(true) }} />}
      {answer.notes?.map((note) => <p key={note}>{note}</p>)}
      {answer.route === "sql" && <p className="muted small">Answered directly from the audit database: exact figures, no AI interpretation.</p>}
      {answer.cached && <p className="muted small">Answered from cache. The same question was asked recently.</p>}
      {answer.route !== "sql" && scores && (
        <details className="answer-more">
          <summary>Quality check: {(scores.average ?? 0).toFixed(2)} of 1.00</summary>
          <div className="scores">
            {SCORES.map(([key, label]) => {
              const value = scores[key] ?? 0;
              return <div key={key}><span>{label}</span><b className="num">{value.toFixed(2)}</b><meter min={0} max={1} low={0.7} optimum={1} value={value} aria-label={label} /></div>;
            })}
          </div>
          {(answer.attempts?.length ?? 0) > 1 && <p className="muted small">Took {answer.attempts!.length} attempts to reach this score.</p>}
        </details>
      )}
      {used.length > 0 && (
        <details className="answer-more" open={sourcesOpen} onToggle={(e) => setSourcesOpen(e.currentTarget.open)}>
          <summary>{used.length === 1 ? "1 source used" : `${used.length} sources used`}</summary>
          <ol className="sources">{used.map((s) => <SourceCard key={s.ref} source={s} id={id} />)}</ol>
        </details>
      )}
      {searched.length > 0 && (
        <details className="answer-more">
          <summary>Also searched, not used ({searched.length})</summary>
          <ol className="sources">{searched.map((s) => <SourceCard key={s.ref} source={s} id={id} />)}</ol>
        </details>
      )}
    </div>
  );
}

export default function Ask() {
  const { data: rows } = useInvoices();
  const [draft, setDraft] = useState("");
  const [scope, setScope] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);

  const ask = useMutation({
    mutationFn: (m: { id: number; question: string }) => slowApi<Answer>("/query", { question: m.question, invoice_id: scope || null }),
    onSuccess: (answer, m) => setMessages((all) => all.map((x) => (x.id === m.id ? { ...x, answer } : x))),
    onError: (error, m) => setMessages((all) => all.map((x) => (x.id === m.id ? { ...x, error: error.message } : x))),
  });

  function send(question: string) {
    const text = question.trim();
    if (text.length < 2 || ask.isPending) return;
    const message = { id: Date.now(), question: text };
    setMessages((all) => [...all, message]);
    setDraft("");
    ask.mutate(message);
  }

  return (
    <>
      <section className="field brand compact section-ask">
        <div className="field-inner"><h1 className="display">Ask about your invoices</h1></div>
      </section>
      <section className="page ask">
        {messages.length === 0 && (
          <div className="examples">
            <p className="muted">Answers come with their sources and a quality score, so you can check them.</p>
            {EXAMPLES.map((e) => <button key={e} className="button quiet" onClick={() => send(e)}>{e}</button>)}
          </div>
        )}
        <div className="thread" aria-live="polite">
          {messages.map((m) => (
            <article key={m.id}>
              <h2 className="question">{m.question}</h2>
              {m.error && <p className="error" role="alert">{m.error}</p>}
              {!m.answer && !m.error && <p className="thinking">Searching invoices and checking the answer…</p>}
              <AnswerBlock message={m} />
            </article>
          ))}
        </div>
        <form className="composer" onSubmit={(e: FormEvent) => { e.preventDefault(); send(draft); }}>
          <label className="sr-only" htmlFor="scope">Search in</label>
          <select id="scope" value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="">All invoices</option>
            {rows?.filter((r) => r.processing_status === "completed").map((r) => <option key={r.invoice_id} value={r.invoice_id}>{r.invoice_number ?? r.file_name}</option>)}
          </select>
          <label className="sr-only" htmlFor="question">Your question</label>
          <input id="question" placeholder="Ask a question about the invoices" maxLength={1000} value={draft} onChange={(e) => setDraft(e.target.value)} />
          <button className="button primary" aria-label="Send question" disabled={ask.isPending || draft.trim().length < 2}><ArrowUp size={18} /></button>
        </form>
      </section>
    </>
  );
}
