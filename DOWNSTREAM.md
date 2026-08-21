# unofficial-codex-rs

This repository is an unofficial, namespaced crates.io downstream of the
OpenAI Codex Rust workspace.

The downstream overlay:

- publishes all local Cargo packages as unofficial-codex-*;
- preserves the upstream library crate names, so Rust source imports do not
  change;
- converts local path dependencies to path + version + package declarations
  so published crates resolve from crates.io;
- adds registry versions to upstream git dependencies that also have a
  crates.io release;
- removes upstream-only websocket patch features that are not present in the
  public crates.io releases;
- preserves the upstream Apache-2.0 license and NOTICE attribution.

The current upstream commit is recorded in UPSTREAM_REVISION. The scheduled
workflow replaces codex-rs/ with the upstream tree, reapplies the overlay,
updates the downstream CalVer, regenerates Cargo.lock, and opens a pull
request for review.

Publishing is tag-driven. The publish workflow authenticates with crates.io
Trusted Publishing and publishes the 119 local packages in dependency order,
waiting for index propagation between retries. Configure the trusted publisher
for this repository before creating an unofficial-codex-v<version> tag. A
workspace release is intentionally not atomic, so a failed run can be rerun.

This project is not affiliated with, endorsed by, or sponsored by OpenAI.
