# Separate source code from the private data vault

Implementation code, schemas, tests, and documentation live in a source repository, while private Evidence Artifacts, recovered databases, generated archives, and credentials live in a separately backed-up data vault. This prevents normal Git and collaboration workflows from accidentally publishing or duplicating sensitive multi-gigabyte personal data.
