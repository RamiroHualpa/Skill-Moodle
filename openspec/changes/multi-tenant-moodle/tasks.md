## 1. Tenant storage foundation (`mcp/moodle/almacen.py`)

- [x] 1.1 Add `tenant_dir(tenant_id=None)`, `mis_datos_path()`, `db_path()`,
      `salidas_dir()` functions replacing the `HOME`/`DB_PATH`/`MIS_DATOS_PATH`/
      `SALIDAS_DIR` module constants; verify by importing the module and calling each
      with an explicit tenant id and with none (falling back to active tenant).
- [x] 1.2 Add `tenant_activo()` / `set_tenant_activo(tenant_id)` reading/writing
      `~/.moodle-skill/estado.json`, defaulting to `"tup"` when the file is absent;
      verify with a unit test that a fresh temp `HOME` returns `"tup"` and that
      `set_tenant_activo` persists across a fresh read.
- [x] 1.3 Add `tenants()` / `registrar_tenant(id, nombre, url)` reading/writing
      `~/.moodle-skill/tenants.json`, defaulting to `[{"id": "tup", ...}]` when
      absent, and rejecting `registrar_tenant` for a duplicate id (raises, does not
      overwrite); verify with a unit test covering the duplicate-id rejection.
- [x] 1.4 Add the idempotent flat-to-`tup/` migration (copies `.env`,
      `mis_datos.json`, `datos.db`, `salidas/` from the old flat `~/.moodle-skill/`
      into `~/.moodle-skill/tup/` if the new path doesn't exist yet; never deletes
      the originals); verify with a unit test using a temp dir seeded with old-layout
      files, asserting the new layout exists and the old files are untouched, and
      that a second run is a no-op.

## 2. Client pool and campus tools (`mcp/server.py`)

- [x] 2.1 Replace the module-global `_cliente`/`_cli()` singleton with a
      `_clientes: dict[str, MobileWSClient]` pool plus per-tenant `asyncio.Lock`,
      keyed by `tenant_id` (default: `almacen.tenant_activo()`); verify existing
      tools still work unchanged (run one read-only tool, e.g. `mis_datos`, against
      the default `tup` tenant with no other tenant configured).
- [x] 2.2 Add `listar_campus()` tool returning every registered tenant (id, nombre,
      url) with the active one marked; verify by calling it on a fresh install and
      confirming it returns exactly the `tup` default.
- [x] 2.3 Add `usar_campus(tenant_id)` tool: validates the id is registered, calls
      `almacen.set_tenant_activo`, returns confirmation; verify it rejects an
      unregistered id without changing the active tenant (call `listar_campus`
      before/after to confirm no change).
- [x] 2.4 Add `agregar_campus(tenant_id, nombre, url, moodle_user, moodle_pass,
      activeia_user="", activeia_pass="")` tool: validates login against `url`
      (reuse the same login-check path `configurar` uses today) before persisting
      anything; on success writes that tenant's `.env` (mode 600),
      `almacen.registrar_tenant(...)`, then runs `descubrir_cursos` +
      `descubrir_comisiones` against the new tenant and saves results to
      `~/.moodle-skill/<tenant_id>/aulas.json` / `comisiones.json`; on invalid
      credentials, persists nothing and returns the failure. Verify both the
      success path (tenant appears in `listar_campus`, its `.env` exists) and the
      failure path (invalid creds → tenant NOT in `listar_campus`, no `.env`
      written) against a real or sandbox Moodle instance.
- [x] 2.5 Refactor `configurar` to share logic with `agregar_campus` via a common
      internal helper, but keep operating implicitly on the active tenant (no new
      required params); verify by calling `configurar` exactly as before (no tenant
      awareness) and confirming identical behavior/output shape to before this
      change.
- [x] 2.6 Update the `aulas.json`/`comisiones.json` read path (currently
      `Path(__file__).parent/...`) to the resolution order: per-tenant
      `~/.moodle-skill/<tenant>/aulas.json` (if present) → repo-shipped catalog
      only when active tenant is `tup` → empty with a message pointing at discovery
      tools; verify with three cases (tup with no override, tup with an override
      file present, a non-tup tenant with no discovered file yet).
- [x] 2.7 Scope `mcp/moodle/navegador.py`'s `_AUTH_DIR` to
      `almacen.tenant_dir(tenant_id)` instead of the flat `MOODLE_SKILL_HOME`;
      verify by running `auditar_aula` (or its underlying browser-session setup)
      against two different tenants in the same process and confirming their
      session/cookie storage paths differ.

## 3. Documentation

- [x] 3.1 Add an "Agregar un campus nuevo" section to `SKILL.md` documenting
      `agregar_campus` / `listar_campus` / `usar_campus` as the guided setup flow
      for a new tenant; verify by re-reading the section against the actual tool
      signatures implemented in section 2.

## 4. End-to-end verification

- [x] 4.1 On a machine with an existing flat single-tenant install, confirm the
      migration runs automatically and every previously-working tool call (at least
      one read tool and, if safe to test, `configurar`) behaves identically to
      before this change.
- [x] 4.2 Register a second tenant end to end via `agregar_campus`, switch to it
      with `usar_campus`, run a read tool (e.g. `pendientes_por_corregir`) against
      it, switch back to `tup`, and confirm the second tenant's data never appeared
      while `tup` was active and vice versa (spec: per-tenant data isolation).
