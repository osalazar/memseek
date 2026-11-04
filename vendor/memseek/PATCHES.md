# Bundled patch: artifact-fulltext-v1

Upstream base: `0930e1077d56d34c3ad2ccc0246fcd180b595b3b`.
This kit applies the adjacent `artifact-fulltext.patch` to `src/memseek/artifacts.py`.
All other upstream runtime files remain unchanged.

Prompt artifact packing now uses the existing full-text `derivation_input` renderer.
The old compact renderer silently shortened each record to 500 characters, even when
its artifact block reported `truncated: false`. Complete records are now measured
against the existing block token budget. A record that does not fit is omitted and
the block reports truncation; a required block that fits nothing fails as before.
Untrusted-text escaping and search preview rendering retain their existing behavior.
The fix applies to native HTTP MCP, stdio MCP, API renders and SDK snapshots.

No stored records, collection schemas or derivations need migration. Existing artifact
snapshots preserve their original bytes. The build marker ends in `.artifact-fulltext-v1`
to distinguish new render/run receipts. Rebuild and recreate API and worker when upgrading.
Regression checks live at `/app/tests/artifact_regression.py` and in the kit's style/MCP suite.

Original file SHA256: `1be88751b0b7e36915601baddcf36b235eaeb5667c031de6097d44f253bd813e`.
Patched file SHA256: `74d27df5c3dee5ad7c82d50676e5d88cb8db35abad1a61ad629f7b26c493a3d9`.
