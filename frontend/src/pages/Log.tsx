import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, RotateCw, ScanLine } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { dateTime, STATUS_LABEL } from "../lib/format";
import { useInvoices } from "./Invoices";

type Health = { status: string; checks: Record<string, string> };

function HealthPanel() {
  const { data, error, isPending } = useQuery({ queryKey: ["system-health"], queryFn: () => api<Health>("/system/health"), refetchInterval: 15_000 });
  if (isPending) return <div className="skeleton-rows" aria-busy="true"><span /><span /></div>;
  if (error) return <p className="error" role="alert">{error.message}</p>;
  return (
    <ul className="health">
      {Object.entries(data.checks).map(([name, state]) => (
        <li key={name} className={state === "ok" ? "ok" : "bad"}><strong>{name.replace("_", " ")}</strong><span>{state === "ok" ? "Connected" : state}</span></li>
      ))}
    </ul>
  );
}

export default function Log() {
  const queries = useQueryClient();
  const { data: rows, isPending, error, refetch, isFetching } = useInvoices();
  const [note, setNote] = useState("");
  const action = useMutation({
    mutationFn: ({ path }: { path: string; done: string }) => api(path, {}),
    onSuccess: (_, v) => { setNote(v.done); queries.invalidateQueries({ queryKey: ["invoices"] }); },
    onError: (e) => setNote(e.message),
  });

  return (
    <>
      <section className="field brand compact section-log"><div className="field-inner"><h1 className="display">Processing log</h1></div></section>
      <section className="page">
        <div className="filters">
          <button className="button primary" disabled={action.isPending} onClick={() => action.mutate({ path: "/process/scan", done: "Scan requested. New files appear here within seconds." })}><ScanLine size={16} aria-hidden="true" /> Scan incoming folder</button>
          <button className="button" disabled={action.isPending} title="Rebuilds the question index from the audit database. No invoice is re-read." onClick={() => action.mutate({ path: "/rag/reindex", done: "Search index rebuild started." })}><RotateCw size={16} aria-hidden="true" /> Rebuild search index</button>
          <button className="button quiet" onClick={() => refetch()}><RefreshCw size={16} aria-hidden="true" className={isFetching ? "spin" : ""} /> Refresh</button>
          <span className="small push" role="status">{note}</span>
        </div>

        {error && <div className="notice error-notice" role="alert"><p>{error.message}</p></div>}
        {isPending && <div className="skeleton-rows" aria-busy="true">{Array.from({ length: 6 }, (_, i) => <span key={i} />)}</div>}
        {rows?.length === 0 && <div className="empty"><h2>No files have arrived yet</h2><p>Every file that reaches the incoming folder is listed here, including ones that can't be processed.</p></div>}
        {rows && rows.length > 0 && (
          <div className="scroll-x">
            <table className="plain log">
              <thead><tr><th>File</th><th>Status</th><th>Attempts</th><th>Stopped at</th><th>Error</th><th>Detected</th><th>Processed</th><th><span className="sr-only">Action</span></th></tr></thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.invoice_id}>
                    <td><Link to={`/invoices/${r.invoice_id}`}>{r.file_name}</Link></td>
                    <td><span className={`status-tag ${r.processing_status}`}>{STATUS_LABEL[r.processing_status]}</span></td>
                    <td className="num">{r.attempts}</td>
                    <td>{r.failed_stage ?? ""}</td>
                    <td className="wrap">{r.error_message ?? ""}</td>
                    <td className="nowrap">{dateTime(r.detected_at)}</td>
                    <td className="nowrap">{dateTime(r.processed_at)}</td>
                    <td>{r.processing_status === "failed" && <button className="button" disabled={action.isPending} onClick={() => action.mutate({ path: `/invoices/${r.invoice_id}/retry`, done: `Retry started for ${r.file_name}.` })}>Retry</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <section aria-labelledby="health"><header className="section-head"><h2 id="health">Service health</h2></header><HealthPanel /></section>
      </section>
    </>
  );
}
