# Use tiered completeness claims

Acceptance distinguishes Source Complete, Extraction Complete, and Reconciliation Complete. Each stronger claim requires its own machine-readable evidence and may fail independently. Extension recovery must account for every IndexedDB store, cursor row, external blob, reference, and extraction error, including any incomplete, failed, truncated, or resumable scan state recorded in metadata. The project never claims Account Complete because available captures cannot prove what the provider account once contained.
