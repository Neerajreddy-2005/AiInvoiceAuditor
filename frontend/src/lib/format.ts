import type { Verdict } from "./types";

export const VERDICT_LABEL: Record<Verdict, string> = { approve: "Approve", review: "Review", reject: "Reject" };
export const VERDICT_HELP: Record<Verdict, string> = {
  approve: "No blocking problems were found.",
  review: "Something needs a person to look at it before payment.",
  reject: "A rule says this invoice must not be paid as it stands.",
};
export const STATUS_LABEL: Record<string, string> = {
  completed: "Processed", processing: "Processing", detected: "Waiting", failed: "Failed", unsupported: "Unsupported file",
};
export const LANGUAGES: Record<string, string> = {
  en: "English", es: "Spanish", de: "German", fr: "French", vi: "Vietnamese",
};
const FIELD_LABELS: Record<string, string> = {
  invoice_number: "Invoice number", invoice_date: "Invoice date", vendor_name: "Vendor", po_number: "Purchase order",
  currency: "Currency", subtotal_amount: "Subtotal", tax_rate_pct: "Tax rate (%)", tax_amount: "Tax amount",
  total_amount: "Total amount", item_code: "Item code", description: "Description", quantity: "Quantity",
  unit_price: "Unit price", line_total: "Line total", line_items: "Line items", line_item: "Line item",
  recommendation: "Recommendation",
};

export const fieldLabel = (field: string | null, line?: number | null) => {
  const text = FIELD_LABELS[field ?? ""] ?? field ?? "";
  return line ? `Line ${line}, ${text.toLowerCase()}` : text;
};

export function money(value: number | string | null | undefined, currency?: string | null) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  const text = number.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return currency ? `${currency} ${text}` : text;
}

export function plain(value: number | string | null | undefined) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number") return value.toLocaleString("en-US", { maximumFractionDigits: 4 });
  return value;
}

export function dateTime(value: string | null | undefined) {
  if (!value) return "—";
  return new Date(value).toLocaleString("en-GB", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function shortDate(value: string | null | undefined) {
  if (!value) return "—";
  return new Date(value).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" });
}
