# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

React, Vite and TypeScript. Supabase Auth for login. The browser talks only to the agent API, never directly to the database.

## Users

Now: the project owner, using it for demos. Build for the planned users: finance and accounts-payable teams who review flagged invoices before payment, often many in one session. Use is mixed, on office desktops by day and in lower light, so both light and dark themes are required, with a switch.

## Product Purpose

Reads incoming invoices in any supported language, checks them against the ERP and business rules, and recommends approve, review or reject. A person makes the final decision. Success means an auditor can see why an invoice was flagged and record a decision quickly, with a trail that shows who did what.

## Positioning

The AI's work is checkable: every recommendation links to the discrepancies, ERP values and source text behind it, and every question answered carries its sources and quality scores.

## Operating Context

Invoices arrive as PDF, DOCX or image files, through a signed-in upload or a watched folder, some in German, Spanish, French or Vietnamese. Auditors compare the invoice value, the ERP value and the expected value, correct what was misread, re-validate, then decide.

## Capabilities and Constraints

- Screens: sign in, invoice list, invoice review (summary, line items, discrepancies, corrections and decision, report, document text, history), questions with sources, processing log.
- Original extracted values are never overwritten. Corrections and decisions are added next to them, with the reviewer taken from the login.
- The system recommends and the person decides; the two are always shown separately.
- Report HTML and invoice text are untrusted content.
- Public sign-up is off; accounts are created in Supabase.

## Evidence on Hand

Sample invoices in `data/incoming`, ERP mock data and real pipeline output. There are no customers, testimonials or benchmarks, and none may be invented.

## Product Principles

- Reason first: the review screen answers "why was this flagged?" before anything else.
- Few clicks from opening an invoice to recording a decision.
- Show the evidence, including original and corrected values side by side.
- Recommendation and human decision never blur together.
- It must look deliberately designed and distinct from a generic admin template, while staying fast for repeated work.
