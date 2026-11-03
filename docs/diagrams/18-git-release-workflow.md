# 18 — Git and release workflow

สามช่วงจาก `git log --first-parent origin/main`: (1) 2026-10-12/13 import + rename (push ตรง), (2) 2026-10-17/18 PR #1–#3 (merge ผ่าน PR, squash), (3) ตั้งแต่ 2026-10-22 commit ตรงเข้า `main` (SPEC-001/002 "Main-only: yes", ADR-0004 rule 2)

```mermaid
gitGraph
  commit id: "d845ec9 initial public release"
  commit id: "f874279 rename"
  branch ci/devsecops-phase1
  commit id: "CI PR"
  checkout main
  merge ci/devsecops-phase1 id: "9dbcb69 #1"
  branch feat/sentry
  commit id: "sentry"
  checkout main
  merge feat/sentry id: "d86b7ce #2"
  branch fix/sentry-no-tracing
  commit id: "no tracing"
  checkout main
  merge fix/sentry-no-tracing id: "79fcdc5 #3"
  commit id: "6560718 L2 baseline"
  commit id: "4e91355 extra key"
  commit id: "ee33256 deploy script"
  commit id: "4d40219 SPEC-002 store"
  commit id: "797a53b latest"
```

```mermaid
flowchart LR
  C["Commit on main, direct"] --> CI["ci.yml push run"]
  C --> CX["Codex review Stop hook, local"]
  CI -->|"green"| WAIT["No deploy"]
  WAIT --> OWN["Owner runs deploy_catalog.py"]
  OWN --> PROD["Railway production"]
  C --> PIN["Consumers pin SHA: store-mirror ab053c9"]
```

Sources: `git log --first-parent origin/main`, `gh pr list -R bookchaowalit/platform-knowledge-api --state all`, `.github/workflows/ci.yml`, `specs/001-l2-baseline/spec.md`, `specs/002-document-store/spec.md`, `scripts/deploy_catalog.py`, architecture-docs `.github/workflows/store-mirror.yml`, ops record: codex-review-stop-hook (2026-11-03), ops record: hosted-document-store (2026-11-03, "Fixes from the Codex review (0b735a0)")

## Open questions

- ไม่มี branch protection / tag / release: ต้องการ tag ต่อ deploy เพื่อรู้ว่า production คือ SHA ใด หรือไม่
