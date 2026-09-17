# LedgerLens v1.0.0 — Offline Document Checker for Accounting Teams

Thank you for choosing LedgerLens. LedgerLens is a 100% offline desktop application designed to detect duplicates, wrong-period filings, corrupt/unreadable files, and missing records in client accounting folders.

All processing occurs strictly on your local PC — zero document content is ever sent to any external server or cloud service.

---

## System Requirements

- **Operating System:** Windows 10 (64-bit) or Windows 11 (64-bit)
- **Processor (CPU):** Intel / AMD 64-bit processor (multi-core recommended)
- **Memory (RAM):** 4 GB minimum (8 GB recommended)
- **Disk Space:** 2.0 GB free disk space
- **Graphics (GPU):** Not required. Fully accelerated for CPU execution.
- **Dependencies:** None. Python, Node.js, Rust, Docker, or command-line tools are **not** required.

---

## Installation Instructions

1. Double-click `LedgerLens_1.0.0_x64-setup.exe`.
2. Follow the standard Windows setup wizard (or choose custom installation directory).
3. Once installation completes, launch **LedgerLens** from your Start Menu or Desktop shortcut.

---

## First-Time Startup & Local AI Setup

On first launch:
1. **System Health Verification:** LedgerLens automatically verifies your local environment, SQLite database, and offline PaddleOCR engine.
2. **Local AI & Ollama (Optional / Automated):**
   - If Ollama or the `qwen2:0.5b` model is missing on your device, click **"Auto-Install / Repair AI Components"** in the System Health screen.
   - LedgerLens will automatically install and configure the lightweight local model in the background without opening any command prompts.

---

## Core Workflow: SCAN → DETECT → REVIEW → REPORT

1. **Scan:** Select a client and choose a local folder containing your client's accounting documents (PDF, CSV, Excel, Word).
2. **Detect:** LedgerLens automatically extracts file hashes, text content, and period metadata to detect issues:
   - Exact byte-for-byte duplicates (SHA-256)
   - Possible/near duplicates (text content similarity)
   - Unreadable, password-protected, or empty files
   - Wrong period documents (e.g. 2021 document in 2024 filing folder)
3. **Review:** Inspect flagged issues side-by-side in the Review Center and record decisions (**Keep**, **Keep Both**, **Ignore**, or **Review Later** with reviewer notes).
4. **Report:** Export clean, audit-ready summary reports in **CSV**, **Excel (.xlsx)**, or **PDF** format.

---

## Privacy & Security Guarantee

- **100% Local Execution:** Documents are read and processed strictly in-place on your filesystem.
- **No Cloud Document Egress:** LedgerLens does not transmit any document text, file hashes, or accounting numbers over the network.
- **Offline Reliability:** All OCR model weights and databases run locally on your device.
