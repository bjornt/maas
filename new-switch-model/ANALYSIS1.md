# Network Interface Model Refactoring — Impact Analysis

## 1. Understanding the Proposed Model Change

The new model proposes splitting the monolithic `maasserver_interface` table into three distinct layers:

| Layer | New Model Section | What It Represents | Example Fields |
|---|---|---|---|
| **Hardware** | `network_cards` + `ports` | Physical NIC cards and their ports — immutable hardware facts | vendor, product, PCI address, MAC, max_speed, link state, transceiver type |
| **Port Config** | `port-config` | How physical ports are wired to the network — the "link layer" | bond membership, untagged VLAN, tagged VLANs, MAC override |
| **Deploy Config** | `default-deploy-config` | Workload-level networking — what gets rendered to netplan/curtin | VLAN sub-interfaces, bridges, IP links (auto/static/dhcp) |

This is a clean separation. Currently, `maasserver_interface` smashes all three
together — a physical NIC, a VLAN sub-interface, a bond, and a bridge are all
rows in the same table differentiated only by a `type` discriminator column.
Hardware properties like `vendor`, `product`, `sriov_max_vf` sit alongside
runtime config like `vlan_id`, `params`, `enabled`.

## 2. What Would Change at the Database Level

The current single table + relationship table would become something like:

```sql
-- Physical hardware (populated during commissioning, rarely changes)
network_card (id, node_id, vendor, product, pci_address, firmware_version, numa_node_id, sriov_max_vf)
network_port (id, card_id, name, index, mac_address, max_speed, port_type, transceiver_type, link_detected, link_speed, link_duplex)

-- Port config (how ports connect to the fabric — set by admin/commissioning)
port_config (id, node_config_id, name, type, mac_address)
port_config_member (id, port_config_id, network_port_id)  -- for bonds
port_config_vlan (id, port_config_id, vlan_id, tagged)     -- untagged + tagged VLANs

-- Deploy config (workload networking — set by user per deployment)
deploy_interface (id, node_config_id, name, type, port_config_id, vid, params)
deploy_interface_ip (id, deploy_interface_id, staticipaddress_id)
deploy_interface_relationship (id, parent_id, child_id)  -- for bridges
```

Plus migration of data from the existing `maasserver_interface` table, the
`maasserver_interfacerelationship` table, and the
`maasserver_interface_ip_addresses` junction table.

## 3. Blast Radius — What Needs to Change

### 3.1 Core Model Layer (Effort: **Very High**)

| Component | Files | Complexity | Notes |
|---|---|---|---|
| Django model (`interface.py`) | 1 file, ~2200 LOC | 🔴 Very High | 6 proxy model classes, custom manager with complex querysets, self-referential M2M, validation logic across types |
| `InterfaceRelationship` model | Same file | 🟡 Medium | Through-model for parent/child — would be restructured |
| `models/__init__.py` re-exports | 1 file | 🟢 Low | Update imports |
| SQLAlchemy table definitions (`tables.py`) | 1 file | 🟡 Medium | Replace `InterfaceTable` + `InterfaceIPAddressTable` with new tables |
| Alembic migration | New file | 🟡 Medium | Data migration from old schema to new, preserving all relationships |
| DB triggers (`0003_register_websocket_triggers.py`) | 1 file | 🔴 High | 4 direct triggers + 5 cross-table triggers that JOIN through `maasserver_interface` — all need rewriting |

### 3.2 Commissioning / Hardware Discovery (Effort: **Very High**)

| Component | Files | Complexity | Notes |
|---|---|---|---|
| `metadataserver/builtin_scripts/network.py` | 1 file, ~800 LOC | 🔴 Very High | The code that creates/updates interfaces from commissioning data. Handles interface migration between nodes, MAC changes, renames, VLAN guessing. Would need to populate the new hardware tables AND port-config |
| `host-info` (Go) | Multiple files | 🟢 Low | Already reports raw hardware info — no changes needed, but you may want to extend its output to include PCI addresses, transceiver types |

### 3.3 Deployment / Preseed (Effort: **High**)

| Component | Files | Complexity | Notes |
|---|---|---|---|
| `maasserver/preseed_network.py` | 1 file, ~750 LOC | 🔴 High | Generates Curtin/Netplan YAML from interfaces. Dispatches by `iface.type` for physical/vlan/bond/bridge. Would read from deploy config layer instead |
| `maasserver/forms/interface.py` | 1 file | 🔴 High | Form classes for each interface type (Physical, Bond, Bridge, VLAN, Controller, Deployed). All validation logic lives here |
| `maasserver/forms/interface_link.py` | 1 file | 🟡 Medium | IP link/unlink forms — would target deploy interfaces |

### 3.4 APIs (Effort: **High**)

| Component | Files | Complexity | Notes |
|---|---|---|---|
| Legacy API (`maasserver/api/interfaces.py`) | 1 large file | 🔴 High | Full CRUD for all interface types + link_subnet, unlink_subnet, set_default_gateway, disconnect. Major rewrite needed |
| v3 API handler (`maasapiserver/v3/.../interfaces.py`) | 3 files | 🟡 Medium | Currently only a list endpoint — less mature, easier to change |
| v3 service layer (`maasservicelayer/services/interfaces.py`) | 1 file | 🟡 Medium | Already has the comment "WIP: We are rethinking the way we model interfaces" |
| v3 repository (`maasservicelayer/db/repositories/interfaces.py`) | 1 file | 🟡 Medium | SQLAlchemy queries against the interface table |
| Pydantic models + builders | 3 files | 🟡 Medium | Request/response models, builders |

### 3.5 WebSocket Handlers (Effort: **High**)

| Component | Files | Complexity | Notes |
|---|---|---|---|
| `websockets/handlers/node.py` | 1 file | 🔴 High | `dehydrate_interface()` — serializes interfaces for the UI. Heavy prefetch queries with joins to subnets/VLANs/fabrics/NUMA |
| `websockets/handlers/machine.py` | 1 file | 🔴 High | Full interface CRUD: create physical/vlan/bond/bridge, delete, link/unlink subnet |
| `websockets/handlers/device.py` | 1 file | 🟡 Medium | Interface CRUD for devices |
| `websockets/handlers/controller.py` | 1 file | 🟢 Low | Read-only VLAN aggregation |

### 3.6 DHCP Subsystem (Effort: **Medium**)

| Component | Files | Complexity | Notes |
|---|---|---|---|
| `maasserver/dhcp.py` | 1 file | 🟡 Medium | `get_best_interface()` — selects bond > physical for DHCP. Logic would need to understand the new port-config layer |
| `maastemporalworker/workflow/dhcp.py` | 1 file | 🟡 Medium | Temporal activity that queries interfaces for DHCP config. Joins `InterfaceTable` directly |
| Go agent (`maasagent/internal/dhcp/`) | Multiple files | 🟢 Low | Only receives `(name, id, vlan_id)` via Temporal — the agent's simplified DQLite model is already decoupled |

### 3.7 DNS Subsystem (Effort: **Low–Medium**)

| Component | Files | Complexity | Notes |
|---|---|---|---|
| `maasserver/dns/config.py` | 1 file | 🟢 Low | Interface → IP → DNS record. Mainly cares about IP addresses on interfaces |
| DNS-related signal handlers | 1 file | 🟡 Medium | `nd_sipaddress_dns_link_notify` triggers walk interface → node → domain |

### 3.8 Other Models That Reference Interface (Effort: **Medium**)

| Model | Relationship | Impact |
|---|---|---|
| `Node.boot_interface` | FK → Interface | Would point to a port-config or deploy-interface instead |
| `NodeDevice.physical_interface` | FK → Interface | Would point to a network_port |
| `Neighbour.interface` | FK → Interface | Needs to reference a port-config (it's about observed ARP on a physical link) |
| `MDNS.interface` | FK → Interface | Similar to Neighbour |
| `ScriptResult.interface` | FK → Interface | Would point to hardware-layer (network_port) |
| `VirtualMachineInterface.host_interface` | FK → Interface | Would point to port-config |
| `BMC/Pod` models | Queryset joins | VLAN matching for pod host interfaces |

### 3.9 Signals (Effort: **Medium**)

| Component | Files | Notes |
|---|---|---|
| `maasserver/models/signals/interfaces.py` | 1 file | Post-save/delete signals for all interface subclasses — trigger DNS updates, DHCP reconfig, auto link-up |

### 3.10 Tests (Effort: **Very High**)

| Area | Files | Notes |
|---|---|---|
| Model tests | `test_interface.py` | Core model tests — full rewrite |
| API tests | `test_interfaces.py` | Legacy API tests |
| Form tests | `test_interface.py`, `test_interface_link.py` | Form validation tests |
| WebSocket tests | `test_device.py`, machine handler tests | UI interaction tests |
| Service layer tests | `test_interfaces.py` (repository + service) | v3 layer tests |
| v3 API tests | `test_interfaces.py` | Handler tests |
| Factory | `testing/factory.py` + `fixtures/factories/interface.py` | Test data creation — needs to create new model objects |

## 4. Effort Estimate

| Phase | Components | Estimated Effort | Risk |
|---|---|---|---|
| Schema design + migration | New tables, Alembic migration, data migration | 2–3 weeks | High — data migration must be lossless |
| Django model layer | New models, managers, validators | 2–3 weeks | High — everything depends on this |
| Commissioning | `network.py` rewrite | 1–2 weeks | Very High — this is the most complex code path |
| Service layer (v3) | Repository, service, builders, Pydantic models | 1–2 weeks | Medium |
| Legacy API | `api/interfaces.py` rewrite | 1–2 weeks | High — backward compatibility |
| WebSocket handlers | node.py, machine.py, device.py | 1–2 weeks | High — UI depends on this |
| Preseed / deployment | `preseed_network.py` | 1 week | High — correctness critical for deployed machines |
| DHCP + DNS + signals + triggers | Multiple files | 1 week | Medium |
| FK updates in other models | ~8 models | 1 week | Medium |
| Tests | All test files | 2–3 weeks | Medium |
| **Total** | | **~12–20 weeks** (1 person) | |

## 5. Phased Approach

Yes, this can absolutely be done in phases. Here's a suggested breakdown:

### Phase 0: Preparation (2–3 weeks)

- **Add new tables alongside the old one** (`network_card`, `network_port`)
  with no FK dependencies yet.
- **Populate hardware tables during commissioning** — write to both old and new
  tables. The commissioning script already receives all the hardware data; you'd
  just also insert it into the new tables.
- **No breaking changes.** The old `maasserver_interface` table continues to be
  the source of truth for everything.
- **Deliverable:** Hardware data is being captured in its own tables. You can
  validate the data model.

### Phase 1: Port Config Layer (3–4 weeks)

- **Create `port_config` tables** alongside the old interface table.
- **Dual-write** during commissioning — populate both old interfaces and new
  port-config records for physical/bond interfaces.
- **Add read paths** in the v3 service layer that can read from the new tables.
- **Keep the old table as the authoritative source** for the legacy API and
  websockets.
- **Deliverable:** Port-config data exists in its own table. The v3 API can
  optionally serve from it.

### Phase 2: Deploy Config Layer (3–4 weeks)

- **Create `deploy_interface` tables.**
- **Migrate the "workload" portion** of interfaces (VLAN sub-interfaces,
  bridges, IP links) to the new deploy config tables.
- **Update preseed generation** to read from the new deploy config.
- **Update the websocket handlers** to dehydrate from the new structure.
- **Deliverable:** Deploy-time networking reads from the new model.

### Phase 3: Cut Over + Deprecation (3–4 weeks)

- **Stop writing to `maasserver_interface`** for new data.
- **Update the legacy API** to translate between old API contract and new tables
  (backward compat).
- **Rewrite DB triggers** for the new tables.
- **Update all FK references** in other models (Neighbour, MDNS, ScriptResult,
  etc.).
- **Final data migration** — move any remaining data, add constraints, drop old
  columns/tables.
- **Deliverable:** Old table is deprecated or removed.

### Phase 4: Cleanup (1–2 weeks)

- Remove dual-write code paths.
- Remove old Django proxy models (or reduce to thin compatibility shims).
- Update all tests.
- Clean up the Go agent's `InterfaceData` DTO if needed (low risk — it's
  already minimal).

## 6. Key Risks and Recommendations

1. **Data migration correctness** — The biggest risk. You need a bulletproof
   mapping from old Interface rows (with their type discriminator, parent/child
   relationships, and VLAN assignments) to the new three-layer model. Write a
   comprehensive migration test that round-trips: old → new → generate preseed,
   and compares the preseed output to what the old model produces.

2. **Backward-compatible API** — The legacy v2 API exposes interfaces as a flat
   list with `type`, `parents`, `children`, `links`. External tools depend on
   this. The new model would need an adapter that reconstructs this view from
   the three layers.

3. **The commissioning script (`network.py`) is the scariest part** — It has
   intricate logic for handling interface renames, MAC address changes,
   interface migration between nodes, and automatic VLAN/fabric discovery. Any
   bug here means machines don't commission correctly.

4. **WebSocket handler dehydration** — The UI relies heavily on the specific
   shape of dehydrated interface data. Changes here need to be coordinated with
   the frontend team.

5. **The dual-write approach mitigates risk significantly** — By writing to both
   old and new tables during phases 0–1, you can validate the new model without
   breaking anything. You can run shadow comparisons to catch discrepancies.

6. **The v3 service layer is your friend** — It already has the comment "WIP: We
   are rethinking the way we model interfaces." Starting the new model in the
   service layer (SQLAlchemy + Pydantic) while keeping the Django layer stable
   is the lowest-risk approach.

7. **The Go agent is almost zero risk** — It only receives `(name, id, vlan_id)`
   via Temporal and doesn't touch PostgreSQL. As long as the Temporal workflow
   continues to send these three fields, the agent doesn't care about the schema
   change.

## 7. Summary

This is a **major, cross-cutting refactoring** touching ~40+ production files
across every layer of MAAS. The total effort is roughly **12–20 engineer-weeks**
depending on how much backward compatibility is required.

However, it's **very amenable to a phased approach**:

- **Phase 0** (hardware tables + dual-write) is low-risk and can be done
  independently
- **Phase 1** (port config) is medium-risk and can be validated via shadow reads
- **Phase 2** (deploy config) is the highest-risk phase — this is where the
  preseed generation and UI paths change
- **Phases 3–4** (cutover + cleanup) are mechanical once the new model is proven

The key insight is that **you don't need to change everything at once**. The
dual-write pattern lets you prove the new model incrementally while the old one
remains authoritative.