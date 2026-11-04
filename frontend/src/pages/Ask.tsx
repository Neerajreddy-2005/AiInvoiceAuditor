import { useMutation } from "@tanstack/react-query";
import { ArrowUp } from "lucide-react";
import { type FormEvent, Fragment, useState } from "react";
import { api } from "../lib/api";
import type { Answer } from "../lib/types";
import { useInvoices } from "./Invoices";

const EXAMPLES = ["Why was INV-1001 flagged?", "How many invoices were rejected?", "What did HafenLogistik invoice?", "Total invoiced per currency"];
const SCORES: [string, string][] = [
  ["answer_relevance", "Answers the question"], ["groundedness", "Supported by sources"], ["context_relevance", "Relevant sources"], ["average", "Average (needs over 0.70)"],
];

type Message = { id: number; question: string; answer?: Answer; error?: string };

function AnswerText({ text, id }: { text: string; id: number }) {
  // Citations like [2] become links to their source; everything else stays plain text.
  return <p className="answer-text">{text.split(/(\[\d+\])/).map((part, i) => {
    const ref = part.match(/^\[(\d+)\]$/)?.[1];
    return ref ? <a key={i} className="cite" href={`#src-${id}-${ref}`}>{ref}</a> : <Fragment key={i}>{part}</Fragment>;
  })}</p>;
}

function AnswerBlock({ message }: { message: Message }) {
  const { answer, id } = message;
  if (!answer) return null;
  const scores = answer.scores;
  return (
    <div className="answer">
      <AnswerText text={answer.answer} id={id} />
      {answer.route === "sql" && <p className="muted small">Answered directly from the audit database: exact figures, no AI interpretation.</p>}
      {answer.cached && <p className="muted small">Answered from cache. The same question was asked recently.</p>}
      {answer.route !== "sql" && scores && (
        <div className="scores">
          {SCORES.map(([key, label]) => {
            const value = scores[key] ?? 0;
            return <div key={key}><span>{label}</span><b className="num">{value.toFixed(2)}</b><meter min={0} max={1} low={0.7} optimum={1} value={value} aria-label={label} /></div>;
          })}
        </div>
      )}
      {(answer.attempts?.length ?? 0) > 1 && <p className="muted small">Took {answer.attempts!.length} attempts to reach this score.</p>}
      {(answer.sources?.length ?? 0) > 0 && (
        <ol className="sources">
          {answer.sources!.map((s) => (
            <li key={s.ref} id={`src-${id}-${s.ref}`}>
              <strong><span className="cite">{s.ref}</span> {s.source_type} · {s.invoice_number ?? "No number"} {s.vendor ? `· ${s.vendor}` : ""}</strong>
              <p>{s.excerpt}</p>
            </li>
          ))}
        </ol>
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
    mutationFn: (m: { id: number; question: string }) => api<Answer>("/query", { question: m.question, invoice_id: scope || null }),
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
