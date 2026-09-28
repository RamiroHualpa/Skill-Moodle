## Purpose

Lets a tutor register, switch between, and operate the existing Moodle-operation
tools against several distinct Moodle campuses (each with its own URL and
credentials) from one local skill installation, without redoing course/comisión
discovery every time they switch.

## ADDED Requirements

### Requirement: Tenant registry
The system SHALL maintain a local registry of configured Moodle campuses (tenants),
each identified by a stable id, a display name, and a base URL, with no credentials
stored in the registry itself.

#### Scenario: Fresh install has the default tenant
- **WHEN** the registry file does not exist yet and any campus-operation tool runs
- **THEN** the system SHALL treat a single tenant with id `tup`, name "TUP (UTN)", and
  the historical default URL as registered and active, without requiring the tutor to
  do anything

#### Scenario: Listing configured tenants
- **WHEN** the tutor asks which campuses are configured
- **THEN** the system SHALL return every registered tenant's id, name, and url, and
  indicate which one is currently active

### Requirement: Adding a new tenant
The system SHALL let a tutor register a new Moodle tenant by supplying an id, a
display name, a base URL, and login credentials, validating the credentials against
that URL before persisting anything.

#### Scenario: Successful registration
- **WHEN** the tutor provides a new tenant id, name, url, and valid credentials for
  that Moodle instance
- **THEN** the system SHALL confirm the login succeeds, persist the credentials
  scoped to that tenant only, add the tenant to the registry, and perform an initial
  discovery of that tenant's courses and comisiones so they are available without a
  separate step

#### Scenario: Invalid credentials are rejected without side effects
- **WHEN** the tutor provides a new tenant id, name, url, and credentials that fail
  to authenticate against that Moodle instance
- **THEN** the system SHALL report the failure and SHALL NOT register the tenant or
  persist any credentials for it

#### Scenario: Duplicate tenant id
- **WHEN** the tutor tries to register a tenant id that is already registered
- **THEN** the system SHALL reject the request rather than silently overwriting the
  existing tenant's configuration

### Requirement: Switching the active tenant
The system SHALL let a tutor set which registered tenant is active, and SHALL
persist that choice so it survives across separate tool invocations within the same
local installation.

#### Scenario: Switching to a registered tenant
- **WHEN** the tutor asks to switch to a tenant id that is registered
- **THEN** the system SHALL make that tenant active for all subsequent
  campus-operation tool calls until changed again

#### Scenario: Switching to an unregistered tenant
- **WHEN** the tutor asks to switch to a tenant id that is not registered
- **THEN** the system SHALL reject the request and leave the currently active tenant
  unchanged

### Requirement: Per-tenant data isolation
The system SHALL keep each tenant's credentials, discovered course/comisión mapping,
tutor-specific "mis datos" configuration, and generated outputs separate from every
other tenant's, such that operating against one tenant never reads or writes another
tenant's data.

#### Scenario: Mapping persists across sessions without rediscovery
- **WHEN** a tenant's courses and comisiones have already been discovered once (at
  registration or by explicit rediscovery)
- **THEN** switching away from and back to that tenant SHALL make the previously
  discovered mapping available immediately, without repeating discovery

#### Scenario: Operating on one tenant does not affect another
- **WHEN** the tutor performs an operation (for example, checking pending
  corrections) against the active tenant
- **THEN** no data belonging to any other registered tenant SHALL be read, modified,
  or exposed in the result

### Requirement: Backward-compatible upgrade for existing installs
The system SHALL preserve the exact current behavior for a tutor who has an existing
single-campus (`tup`) configuration and never uses the new multi-tenant tools,
migrating their existing data into the new layout automatically and without loss.

#### Scenario: Existing single-campus data migrates automatically
- **WHEN** the skill runs for the first time after upgrading, on an installation that
  has pre-existing single-campus configuration and data
- **THEN** the system SHALL make that data available under the `tup` tenant without
  requiring any manual action, and without deleting the original files

#### Scenario: Existing tool calls keep working unchanged
- **WHEN** a tutor who has never registered an additional tenant calls any existing
  campus-operation tool the same way they did before this change
- **THEN** the tool SHALL behave exactly as before, operating against the `tup`
  tenant by default
