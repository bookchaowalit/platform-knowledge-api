# 15 — CI/CD pipeline

workflow เดียว `.github/workflows/ci.yml`; ไม่มี deploy job (ไม่ได้ต่อ CD, ดู ADR-0002)

```mermaid
flowchart TD
  T(["push main, pull_request, workflow_dispatch; concurrency ci-ref, cancel in progress; permissions contents read"])
  T --> API["api: platform-ci-workflows ci-python.yml at 87dc5e1c v1.1.1, python 3.12, coverage 88"]
  T --> SEC["security: platform-ci-workflows security.yml at 87dc5e1c v1.1.1"]
  API --> A1["Checkout"] --> A2["Set up uv and Python"] --> A3["Install: uv sync --frozen --all-extras"]
  A3 --> A4["Lint Ruff"] --> A5["Typecheck mypy"] --> A6["Test pytest --cov, embedded pgserver Postgres"] --> A7["Coverage gate 88"]
  SEC --> S1["gitleaks"]
  SEC --> S2["semgrep"]
  SEC --> S3["osv-scanner"]
  SEC --> S4["trivy"]
  A7 -.->|"no deploy link"| MAN["Manual: deploy_catalog.py, railway up --no-gitignore"]
  REN["Renovate: Monday before 9am Asia/Bangkok, groups platform-ci-workflows and python non-major"] -.-> T
```

Sources: `.github/workflows/ci.yml`, `bookchaowalit/platform-ci-workflows` `.github/workflows/ci-python.yml` and `security.yml` at `87dc5e1c` (step names via `gh api`), `pyproject.toml` (dev deps `pgserver`), `specs/002-document-store/spec.md` §6, `renovate.json`, `scripts/deploy_catalog.py`

ภายนอก repo แต่ใช้โค้ด repo นี้: architecture-docs `store-mirror.yml` checkout repo นี้ที่ SHA pin `ab053c9` แล้วรัน `store_sync.py` (ดู 10-D)

## Open questions

- CI ไม่ build Docker image: Dockerfile พังจะรู้ตอน `railway up` เท่านั้น
- Renovate เปิด PR จริงหรือไม่ (`gh pr list` มีแค่ PR #1–#3 ของ owner)
