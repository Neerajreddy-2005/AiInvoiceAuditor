import { useQueryClient } from "@tanstack/react-query";
import { Upload } from "lucide-react";
import { useRef, useState } from "react";
import { uploadInvoice } from "../lib/api";

export function UploadButton() {
  const input = useRef<HTMLInputElement>(null);
  const queries = useQueryClient();
  const [progress, setProgress] = useState<string | null>(null);
  const [message, setMessage] = useState<{ text: string; failed: boolean } | null>(null);

  async function upload(files: File[]) {
    setMessage(null);
    const failures: string[] = [];
    for (const [index, file] of files.entries()) {
      setProgress(files.length > 1 ? `Uploading ${index + 1} of ${files.length}…` : "Uploading…");
      try {
        await uploadInvoice(file);
      } catch (error) {
        failures.push(`${file.name}: ${(error as Error).message}`);
      }
    }
    setProgress(null);
    await queries.invalidateQueries({ queryKey: ["invoices"] });
    const added = files.length - failures.length;
    if (failures.length) setMessage({ text: failures.join(" "), failed: true });
    else setMessage({ text: `${added} ${added === 1 ? "invoice" : "invoices"} added. Checking now.`, failed: false });
  }

  return (
    <>
      <input
        ref={input} type="file" multiple hidden
        accept=".pdf,.docx,.png,.jpg,.jpeg,.tif,.tiff"
        onChange={(e) => {
          const files = [...(e.target.files ?? [])];
          e.target.value = ""; // so choosing the same file again still fires
          if (files.length) void upload(files);
        }}
      />
      <button className="button primary" disabled={progress !== null} onClick={() => input.current?.click()}>
        <Upload size={18} aria-hidden="true" />{progress ?? "Upload invoice"}
      </button>
      <span className={`upload-message small ${message?.failed ? "error" : "muted"}`} role="status">{message?.text}</span>
    </>
  );
}
