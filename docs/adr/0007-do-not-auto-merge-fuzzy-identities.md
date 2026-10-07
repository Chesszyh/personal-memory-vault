# Do not automatically merge fuzzy identities

Records with stable native identifiers may merge deterministically; title, text, and timestamp similarity may only create an Identity Match Candidate for review. This sacrifices some automatic deduplication because a false merge is harder to detect and reverse than an explicit unresolved duplicate.
