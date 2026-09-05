# v0.1.1 native-asset completeness incident

The v0.1.1 release passed all six local Docker oracles and all six registry
oracles, each with zero exceptions/retries. Both jobs were independently admitted.
The HF commit `a1de6b0c9748adccbfc0a96110f998a974cb4708` contains 590 verified
Git/LFS objects. Registry identifier:
`sha256:65aab080467da8106db961be43f201e8df58bb6009e324c86730f506532f8925`.

The website generator then compared task asset `sha256` values with binary
download hashes and stopped. The shared author's `asset()` intentionally hashes
UTF-8 content; it does not hash the rendered PDF/XLSX container. Binary manifests
and remote object receipts were correct. This was not a corrupt upload.

Inspection found a separate completeness defect: the shared PDF writer used
`line[:92]` and at most 48 lines. Nine of fifteen PDFs lost evidence text: all six
current policies and all three document artifacts for task006. The runtime API
still returned the complete source text, so oracle passes did not expose this.

Patch 0.1.2 leaves the vendored authoring code unchanged and uses a release-only
writer that wraps long lines and emits additional pages. Independent `pypdf`
extraction checks all fifteen PDFs plus escaping, long text and multiple pages.
XLSX cells are checked against every source row. Metadata now labels text and
binary digests explicitly, and frozen verification checks both.

The old commits, tags, files and successful runtime receipts remain historical
evidence. Do not present v0.1.1 as native-document-complete or silently relabel
its receipts. New package bytes require new clean local and registry runs before
the website uses them. No model result is established by either oracle run.
