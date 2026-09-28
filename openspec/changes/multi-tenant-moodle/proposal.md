## Why

Today the skill assumes exactly one Moodle instance: one global client, one `.env`, one
flat `~/.moodle-skill/` data directory. The tutor now needs to operate against several
distinct Moodle campuses (different domains, different credentials, different
comisiones/materias), switching between them without losing the mapping work already
done for each.

## What Changes

- New on-disk registry (`~/.moodle-skill/tenants.json`) listing configured campuses
  (id, nombre, url) — no secrets in this file.
- New on-disk active-tenant pointer (`~/.moodle-skill/estado.json`).
- Per-tenant data directories (`~/.moodle-skill/<tenant_id>/`) replacing the current
  flat layout for `.env`, `mis_datos.json`, `datos.db`, `salidas/`.
- Automatic one-time migration of the existing flat `tup` data into
  `~/.moodle-skill/tup/` on first run after upgrade — no manual steps, no data loss,
  identical behavior for a tutor who never touches the new feature.
- New MCP tools: `listar_campus`, `usar_campus`, `agregar_campus` (the latter validates
  login against the new campus, persists its `.env`, and runs the existing
  `descubrir_cursos`/`descubrir_comisiones` once to seed that tenant's own
  `aulas.json`/`comisiones.json`).
- `configurar` (existing tool) refactored to operate implicitly on the active tenant
  (default `tup`), preserving its current external behavior exactly for existing
  callers.
- Client pool (`_clientes: dict[str, MobileWSClient]`) replacing the single global
  `_cliente` singleton, keyed by tenant id, so every existing tool (all ~50 of them,
  which call the client getter with no arguments) automatically operates against
  whichever tenant is currently active — no signature changes needed on existing tools.
- `aulas.json`/`comisiones.json` resolution order: per-tenant discovered file first,
  then (only for `tup`) the repo-shipped curated catalog as fallback, then empty +
  guidance to run discovery.

**BREAKING**: none for existing single-campus usage — the migration and defaults
preserve current behavior. Internal-only breaking change: `almacen.py`'s
`MIS_DATOS_PATH`/`DB_PATH`/`SALIDAS_DIR` module-level constants become functions
(any external code importing them as constants would need updating; none is known
outside this repo).

## Capabilities

### New Capabilities
- `multi-tenant-moodle`: registering, listing, switching between, and persisting
  connection data and discovered course/comisión mappings for multiple distinct Moodle
  instances, with one being "active" at a time and all existing campus-operation tools
  transparently operating against it.

### Modified Capabilities
(none — no existing spec-tracked capability in this repo yet; this is the first
tracked capability)

## Impact

- `mcp/server.py`: client singleton → pool; new tools `listar_campus`/`usar_campus`/
  `agregar_campus`; `configurar` refactor; `aulas.json`/`comisiones.json` resolution
  logic.
- `mcp/moodle/almacen.py`: path constants → tenant-parameterized functions; new
  `tenant_activo()`/`set_tenant_activo()`/`tenants()`/`registrar_tenant()`; one-time
  migration logic for the flat-to-`tup/` layout.
- `mcp/moodle/navegador.py`: `_AUTH_DIR` becomes tenant-scoped (browser session
  cookies for `auditar_aula` must not leak across tenants).
- `SKILL.md`: new "Agregar un campus nuevo" section documenting the guided flow.
- Downstream, separate repo `asistente-tup` (own project/openspec scope) reads
  `mis_datos.json` directly and will need matching updates to become tenant-aware;
  tracked as its own change in that repo, not part of this proposal's tasks.
