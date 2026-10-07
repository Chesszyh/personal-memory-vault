# Copy and verify evidence before cutover

Private source captures will first be copied into the external data vault, inventoried in Evidence Manifests, and verified byte for byte. The current copies remain untouched until the resulting archive passes its acceptance gate and the user separately authorizes cleanup. Browser recovery and parsers operate only on named Recovery Working Copies, never through symbolic links to a Master Evidence Copy. Private recovery data is ignored by Git immediately and is never committed.
