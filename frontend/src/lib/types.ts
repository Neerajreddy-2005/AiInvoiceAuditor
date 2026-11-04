export type Verdict = "approve" | "review" | "reject";

export interface InvoiceRow {
  invoice_id: string;
  file_name: string;
  invoice_number: string | null;
  vendor_name: string | null;
  invoice_date: string | null;
  currency: string | null;
  total_amount: number | null;
  recommendation: Verdict | null;
  processing_status: "detected" | "processing" | "completed" | "failed" | "unsupported";
  attempts: number;
  failed_stage: string | null;
  error_message: string | null;
  detected_at: string;
  processed_at: string | null;
  top_reason: string | null;
  decided: boolean;
  decision: Verdict | null;
}

export interface Invoice extends Omit<InvoiceRow, "top_reason" | "decided" | "decision"> {
  extraction_method: string | null;
  raw_extracted_text: string | null;
  translated_text: string | null;
  source_language: string | null;
  translation_confidence: number | null;
  po_number: string | null;
  subtotal_amount: number | null;
  tax_rate_pct: number | null;
  tax_amount: number | null;
}

export interface LineItem {
  line_number: number;
  item_code: string | null;
  description: string | null;
  quantity: number | null;
  unit_price: number | null;
  line_total: number | null;
}

export interface Run {
  run_id: number;
  run_number: number;
  trigger: string;
  triggered_by: string | null;
  recommendation: Verdict | null;
  summary: string | null;
  recommendation_reasons: string[] | null;
  completed_at: string | null;
}

export interface Discrepancy {
  id: number;
  severity: "high" | "medium" | "low";
  field_name: string;
  line_number: number | null;
  invoice_value: string | null;
  erp_value: string | null;
  expected_value: string | null;
  deviation_pct: number | null;
  message: string | null;
}

export interface Feedback {
  id: number;
  feedback_type: "correction" | "decision" | "comment";
  field_name: string | null;
  line_number: number | null;
  original_value: string | null;
  corrected_value: string | null;
  comment: string | null;
  corrected_by: string;
  corrected_at: string;
  revalidated: boolean;
}

export interface InvoiceDetail {
  invoice: Invoice;
  line_items: LineItem[];
  runs: Run[];
  latest_run_id: number | null;
  discrepancies: Discrepancy[];
  feedback: Feedback[];
}

export interface Source {
  ref: number;
  source_type: string;
  invoice_id?: string;
  invoice_number: string | null;
  vendor: string | null;
  po_number?: string | null;
  file_name?: string | null;
  excerpt: string;
}

export interface Answer {
  answer: string;
  route?: string;
  cached?: boolean;
  scores?: Record<string, number>;
  attempts?: { attempt: number; query: string; scores: { average: number } }[];
  sources?: Source[];
  rows?: Record<string, string | number | null>[];
  notes?: string[];
}
