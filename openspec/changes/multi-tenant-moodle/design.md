## Context

See proposal.md - Why. Current state: `mcp/server.py` holds one module-global
`_cliente: MobileWSClient | None`, lazily built by `_cli()` from `os.environ`
(populated from `~/.moodle-skill/.env`, written by `_escribir_env()`). Path constants
in `mcp/moodle/almacen.py` (`HOME`, `DB_PATH`, `MIS_DATOS_PATH`, `SALIDAS_DIR`) are
computed once at import time from a single flat `~/.moodle-skill/` directory.
`aulas.json`/`comisiones.json` are static, repo-shipped, curated catalogs for the
`tup` campus specifically (`Path(__file__).parent/...`), not per-tutor data.
`MobileWSClient.__init__(base_url, usuario, password)` has no module-level shared
state, so multiple instances are already safe to hold concurrently.

We already reviewed a prior multi-tenant system (`moodle-copiloto`) that solves a
different axis — many tutors sharing one Moodle — and reuse only its general shape
(a keyed pool with lazy init + lock), not its code, since its data model (SQLite,
Fernet-encrypted vault, `X-Tutor-Id` request header) doesn't fit a single-tutor local
process.

## Goals / Non-Goals

**Goals:**
- Zero behavior change for a tutor who never touches the new tools (default tenant
  `tup`, automatic one-time migration).
- Adding/switching tenants without editing config files by hand.
- Discovered course/comisión mapping persisted per tenant, computed once.
- No signature changes to the ~50 existing MCP tools.

**Non-Goals:**
- No encryption at rest for credentials (matches current `tup` behavior: file
  permissions only, decided explicitly with the user — see plan discussion).
- No concurrent multi-tutor support (that's `moodle-copiloto`'s problem, not this
  one) — this stays a single local tutor operating several tenants serially.
- No changes to `asistente-tup` in this change; tracked separately in that repo.

## Decisions

**Active-tenant model over per-call `tenant_id` parameter.** With ~50 existing tools
all calling `_cli()` with no arguments, threading a `tenant_id` param through every
one would be large, mechanical, and error-prone (a caller forgetting to pass it would
silently hit the wrong campus). An active-tenant pointer read once per `_cli()` call
means existing tools need zero changes. Trade-off: state is now "hidden" (which
tenant is active isn't visible in a single tool call's arguments) — mitigated by
`listar_campus` always showing which one is active, and every "operate on campus"
tool response should be able to name which tenant it ran against for the calling LLM
to surface to the tutor if relevant.

**Per-tenant directories over a single tenant-keyed file/DB.** Chosen over (a) adding
a `tenant_id` column to a shared SQLite DB, or (b) one big JSON with tenants as top-
level keys. Directories keep every existing per-tutor file format unchanged
(`mis_datos.json`'s shape, `datos.db`'s schema, `salidas/` as a real directory) — only
the path prefix changes. This avoids a schema migration for `datos.db` and lets the
`tup` migration be a straight file/directory copy instead of a data transform.

**`.env` permissions only, no Fernet.** Explicit user decision: matches current `tup`
behavior, avoids introducing a master-key-management concern for a single local
tutor's own machine. Revisit only if this skill is ever run somewhere shared.

**`aulas.json`/`comisiones.json` resolution order (per-tenant cache → `tup` curated
fallback → empty)** rather than requiring every tenant, including `tup`, to run
discovery. Preserves `tup`'s hand-curated, presumably more complete/accurate catalog
as the default, while still letting a tutor re-run discovery for `tup` too (their
override, once present, always wins over the shipped catalog).

**`agregar_campus` runs discovery inline** rather than requiring a separate manual
step, directly per the user's requirement that the initial mapping happen once and be
persisted without extra back-and-forth. If discovery partially fails (e.g. some
courses inaccessible), the tenant is still registered (login already succeeded) and
the tutor can re-run discovery tools manually afterward — registration is not rolled
back by a discovery failure.

## Risks / Trade-offs

- [Migration runs against live production data on first upgrade] → Migration only
  *copies* (never deletes/moves) the old flat files into `tup/`; old paths stay
  present and functional as a fallback read source until fully proven out, so a bug
  in the new per-tenant path resolution degrades to old behavior rather than data
  loss.
- [A tutor forgets which tenant is active and unknowingly operates on the wrong one]
  → `listar_campus` output always marks the active tenant; tool responses that touch
  campus data should name the tenant/URL they ran against.
- [Browser-based tools (`auditar_aula`, using `navegador.py`) carry stale session
  cookies across a tenant switch if `_AUTH_DIR` scoping is missed] → explicitly listed
  as its own task; verified by testing `auditar_aula` after a tenant switch.
- [Duplicate/typo'd tenant ids silently shadow an existing tenant] → `agregar_campus`
  explicitly rejects a duplicate id (see spec) rather than overwriting.

## Migration Plan

1. Ship the migration as an idempotent check at skill startup (or lazily, first time
   any path-resolution function runs): if `~/.moodle-skill/.env` exists (old flat
   layout) and `~/.moodle-skill/tup/.env` does not, copy `.env`, `mis_datos.json`,
   `datos.db`, `salidas/` into `~/.moodle-skill/tup/`. Create `tenants.json`
   (seeded with `tup`) and `estado.json` (`tenant_activo: "tup"`) if absent.
   Idempotent: safe to run on every startup, no-ops once migrated.
2. No rollback needed beyond "don't delete the old flat files" (already the plan) —
   if something goes wrong, the tutor's original data is untouched at the old paths.

## Open Questions

- Exact naming/casing convention for tutor-supplied tenant ids (free text vs.
  slugified automatically from the name) — default to slugifying the supplied name if
  no explicit id is given, low-risk to decide during implementation.
