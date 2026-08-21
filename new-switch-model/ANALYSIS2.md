# Interface Model Refactor — Effort Analysis

Based on reading `NETWORK-MODEL.md`, `model-old.yaml`, `new-model.yaml`, and a
survey of the MAAS codebase.

## What the new model actually proposes

From `new-model.yaml` the split is three layers:

1. **Hardware** (`network_cards` + `ports`) — vendor, product, PCI, MAC,
   max/link speed, port type, transceiver, NUMA, SR-IOV. Facts from
   commissioning, essentially append-only.
2. **Port config** — how a physical port is wired into the fabric
   (tagged/untagged VLANs) and how ports are grouped into bonds. This is
   "physical connectivity."
3. **Deploy config** (`default-deploy-config`) — VLAN interfaces, bridges, IP
   links. What the deployed OS will actually run.

Today all three are mashed into `maasserver_interface` with proxy models
(`PhysicalInterface`, `BondInterface`, `BridgeInterface`, `VLANInterface`,
`UnknownInterface`) sharing one row schema plus a JSON `params` field and the
`InterfaceRelationship` parent/child M2M.

## Scope of what depends on the current table

A grep across `src/` (excluding migrations) turns up **169 Python files and
~108 UI files** touching Interface. The heavy hitters:

| Area | Files | Notes |
|---|---|---|
| Django model | `maasserver/models/interface.py` (2216 LOC) | Interface + 5 proxy subclasses + InterfaceRelationship |
| REST API | `maasserver/api/interfaces.py` (1174), `api/nodes.py`, `api/machines.py`, `api/devices.py` | Public, strong back-compat requirement |
| Forms | `forms/interface.py` (783), `forms/interface_link.py` | PhysicalInterfaceForm/BondInterfaceForm/BridgeInterfaceForm/VLANInterfaceForm |
| Commissioning | `metadataserver/builtin_scripts/network.py`, `hooks.py` | Writes the rows from LXD data |
| Preseed/curtin | `maasserver/preseed_network.py` (792) | Generates Curtin network-config for deploys |
| DHCP | `maasserver/dhcp.py`, `maastemporalworker/workflow/dhcp.py` | Reads interfaces → dhcpd.conf |
| DNS | `maasserver/dns/config.py` | Iterates `current_config.interface_set.all()` |
| Websockets | `handlers/machine.py`, `controller.py`, `node.py`, `device.py` | Shapes payload UI consumes |
| Service layer v3 | `maasservicelayer/{models,services,db/repositories}/interfaces.py`, `maasapiserver/v3/...` | ~800 LOC, parallel to legacy |
| Pod drivers | `provisioningserver/drivers/pod/*` | VM interface discovery/compose |
| Triggers/signals | `models/signals/interfaces.py`, DB triggers for websocket notifies | Keyed on the single table |
| Tests | ~78 files | Plus factories in `testing/factory.py` |
| UI | `maasui/src/src/app/**` | `app/machines`, `app/base/components/NetworkActionRow`, NodeSummaryNetworkCard, etc. |

## Can it be phased? Yes — the layered split is naturally phased

The trick is to keep the current `Interface` Django model alive as a **read
facade** over the new tables during the transition. Because reads dominate
(preseed, DHCP, DNS, websockets, UI all mostly read), you can migrate writers
one at a time while readers see the same shape.

### Phase 0 — Foundation (1–2 weeks)

Lock the ER diagram from the yaml; decide whether to keep `maasserver_interface`
as a view/materialized view backed by the new tables, or cut it and shim in
Python. Finish the new→old converter that's already in this playground repo.

### Phase 1 — Hardware extraction (4–8 weeks, LOW risk)

New tables: `NetworkCard` (vendor, product, pci_address, firmware, numa_node) +
`NetworkPort` (name, mac, max_speed, link_detected, link_speed, link_duplex,
port_type, transceiver, sriov_max_vf).

- Backfill from `maasserver_interface` where `type='physical'`.
- Dual-write from commissioning (`metadataserver/builtin_scripts/network.py`),
  with the old columns as the source of truth during the phase.
- Interface model keeps properties like `.vendor`, `.link_speed` that read from
  the new tables.
- No changes to API, forms, preseed, UI.
- Can ship alone.

### Phase 2 — Port config (6–10 weeks, MEDIUM risk)

New table `PortConfig` for what's attached to a physical port (untagged_vlan,
tagged_vlans) and bond membership. This is where the invariant "bonds have
member ports, bonds sit on fabrics" gets cleaned up.

- Migrate `params` JSON (bond_mode etc.) off Interface onto PortConfig.
- Forms rewrite is concentrated in `forms/interface.py` (BondInterfaceForm,
  PhysicalInterfaceForm).
- Commissioning writes via the new layer; preseed/DHCP still read via the
  facade.

### Phase 3 — Deploy config split (8–12 weeks, HIGH risk)

New table `DeployConfig` for VLAN/bridge interfaces + IP links.

- This is the dangerous phase — it drives what actually gets deployed.
- `preseed_network.py`, DHCP snippets, DNS config all move to the new model.
- VLANInterfaceForm / BridgeInterfaceForm / AcquiredBridgeInterfaceForm
  rewrite.
- The `acquired` bridge cleanup on machine release is a subtle invariant worth
  writing a test for first.

### Phase 4 — Websocket/UI cutover (4–6 weeks cheap / 2–4 months expensive)

- Cheap: keep the websocket serializer producing today's `interfaces` shape
  forever. UI untouched.
- Expensive: redesign the Networks panel around the three-layer model. This is
  a multi-month UI effort across ~100 UI files; only worth doing if product
  wants to expose the layered concept.

### Phase 5 — Cleanup (2–4 weeks)

Drop `maasserver_interface`, delete the facade, remove proxy models.

## Rough total effort

- **Hardware-only split (Phase 0+1+5):** ~2 months of one engineer. Ships a
  real improvement, can stop here if priorities shift.
- **Full backend split, UI preserved (Phases 0–3 + 5, skip 4):** ~6 months of
  one engineer, or ~4 months with two.
- **Full split including UI redesign:** ~8–10 months.

## Risks worth flagging now

- **Public REST API** — `op=create_physical/create_bond/...` is part of the
  contract. Plan to keep it and translate at the edge, not bump to a v4.
- **InterfaceRelationship graph** — commissioning's `sorttop`-based dependency
  walk and preseed's bridge-over-bond-over-vlan traversal rely on a single
  parent/child graph. The new model needs an equivalent graph API or those
  algorithms get rewritten.
- **Postgres triggers** that drive websocket notifications are keyed on the
  single table; each new table needs its own.
- **NodeConfig coupling** — current vs. deployed state is modeled via
  `NodeConfig`. Hardware should live on the node (it doesn't change per
  config), but port-config/deploy-config stay per-NodeConfig. That boundary is
  worth pinning down in Phase 0.
- **Device nodes** — `Device` reuses `Interface` without NUMA/hardware, so
  whatever shim you build must tolerate "hardware layer absent."

## Bottom line

Phased is realistic. Phase 1 (hardware extraction) is a low-risk standalone
win that can start immediately to validate the approach before committing to
the bigger Phase 3 deploy-config rewrite.
