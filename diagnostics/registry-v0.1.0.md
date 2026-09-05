# Registry execution failure: package v0.1.0

On 2026-09-05, the complete six-task oracle job fetched the published Harbor
dataset at `sha256:4d3737ff3fb16fc8f9b4ec54f75c19021435973fd624ad27ee54e88e459173db`.
All six downloaded package digests matched the qualified local freeze exactly.
The registry job did **not** qualify: task004 passed, tasks002/003 failed separate
verification, and tasks001/005/006 failed world startup. Harbor's CLI returned
exit code 0 despite these failures; admission rejected the job.

A bounded DockerEnvironment inspection found task001 and task002 world images
with the correct task.json bytes but identity.json from task003. Task001's
actual task SHA256 was `f1240a00309f14229bcaa03a748ed550e8e2e4396edd24a56e252bc860d17f4e`;
the stale identity instead committed to task003's
`582ce536080d1904a9234ab089c57354f586e84f77413d4175040f0600aad84e`.
The runtime correctly failed closed with
`world projection does not match frozen identity`. A world-only task001 compose
build was healthy and contained the correct identity. Scoped diagnostic
containers, networks and volumes were removed; failed job evidence was retained.

The observed problem is mixed inputs inside built containers, not corrupted
downloaded archives. Docker/BuildKit context reuse with normalized epoch mtimes
and equal-sized metadata is a hypothesis, not an established internal cause.
Package 0.1.1 therefore gives each image a content-addressed input archive and
checks its SHA256 at build time. This does not weaken any runtime identity guard.
All six tasks must pass again from clean frozen bytes, both locally and through
the registry, before 0.1.1 can be described as qualified. Do not overwrite the
v0.1.0 tag or attach new success claims to its failed registry execution.
