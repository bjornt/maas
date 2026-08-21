#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pyyaml",
# ]
# ///
"""Convert a new-format network model YAML to the old format.

Usage:
    uv run convert-to-old-model.py new-model.yaml
    uv run convert-to-old-model.py new-model.yaml -o converted.yaml
    uv run convert-to-old-model.py new-model.yaml --machine machine-simple
"""

import argparse
import re
import sys
from pathlib import Path

import yaml


def main():
    parser = argparse.ArgumentParser(
        description="Convert a new-format network model YAML to the old format.",
    )
    parser.add_argument("input_file", help="Path to the new-format YAML model file")
    parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: stdout)",
        default=None,
    )
    parser.add_argument(
        "-m",
        "--machine",
        help="Convert only the specified machine",
        default=None,
    )
    args = parser.parse_args()

    path = Path(args.input_file)
    if not path.exists():
        print(f"Error: file not found: {args.input_file}", file=sys.stderr)
        sys.exit(2)

    try:
        with open(path) as f:
            new_model = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"Error: invalid YAML: {e}", file=sys.stderr)
        sys.exit(2)

    if not new_model or "machines" not in new_model:
        print("Error: input file does not contain a 'machines' key", file=sys.stderr)
        sys.exit(2)

    machines = new_model["machines"]

    if args.machine:
        if args.machine not in machines:
            print(
                f"Error: machine '{args.machine}' not found in input file",
                file=sys.stderr,
            )
            sys.exit(2)
        machines = {args.machine: machines[args.machine]}

    old_model = {"machines": {}}
    for machine_name, machine_data in machines.items():
        old_model["machines"][machine_name] = _convert_machine(machine_data)

    output = yaml.dump(
        old_model,
        Dumper=_OldModelDumper,
        default_flow_style=False,
        sort_keys=False,
    )

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
    else:
        sys.stdout.write(output)


# ---------------------------------------------------------------------------
# YAML Dumper that avoids quoting MAC addresses
# ---------------------------------------------------------------------------

# Integer pattern without the sexagesimal alternative (which matches
# colon-separated digits like MAC addresses).  The original PyYAML
# pattern includes ``[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+`` as one
# alternative — we simply drop that branch.
_INT_NO_SEXAGESIMAL = re.compile(
    r"^(?:[-+]?0b[0-1_]+"
    r"|[-+]?0[0-7_]+"
    r"|[-+]?(?:0|[1-9][0-9_]*)"
    r"|[-+]?0x[0-9a-fA-F_]+)$",
    re.VERBOSE,
)


class _OldModelDumper(yaml.SafeDumper):
    """Custom dumper that replaces the YAML 1.1 sexagesimal integer resolver.

    Without this, MAC addresses whose octets are all <= 59 (e.g.
    ``00:11:22:33:44:55``) would be quoted because PyYAML thinks they
    could be parsed as base-60 integers.
    """


# Copy the resolver table, replacing the int pattern with one that
# excludes the sexagesimal alternative.
_OldModelDumper.yaml_implicit_resolvers = {}
for _key, _resolver_list in yaml.SafeDumper.yaml_implicit_resolvers.items():
    _new_list = []
    for _tag, _regexp in _resolver_list:
        if _tag == "tag:yaml.org,2002:int" and ":[0-5]" in _regexp.pattern:
            _new_list.append((_tag, _INT_NO_SEXAGESIMAL))
        else:
            _new_list.append((_tag, _regexp))
    _OldModelDumper.yaml_implicit_resolvers[_key] = list(_new_list)


# ---------------------------------------------------------------------------
# VLAN fabric helpers
# ---------------------------------------------------------------------------

def _build_vid_fabric_map(port_config: dict) -> dict[int, int]:
    """Build a mapping from VID to fabric ID by scanning port-config entries.

    Inspects ``untagged_vlan`` and ``tagged_vlans`` on each port-config
    entry.  Both are expected to be dicts with ``vid`` and ``fabric`` keys.
    """
    vid_fabric: dict[int, int] = {}
    for config in port_config.values():
        untagged = config.get("untagged_vlan")
        if isinstance(untagged, dict):
            vid_fabric[untagged["vid"]] = untagged["fabric"]

        for entry in config.get("tagged_vlans", []):
            if isinstance(entry, dict):
                vid_fabric[entry["vid"]] = entry["fabric"]
    return vid_fabric


# ---------------------------------------------------------------------------
# Name mapping helpers
# ---------------------------------------------------------------------------

def _build_name_map(port_config: dict) -> dict[str, str]:
    """Build a mapping from abstract names to real port names.

    Scans ``port-config`` entries for a ``port_name`` field and returns
    a dict like ``{"iface1": "eth0", ...}``.
    """
    name_map: dict[str, str] = {}
    for abstract_name, config in port_config.items():
        port_name = config.get("port_name")
        if port_name:
            name_map[abstract_name] = port_name
    return name_map


def _translate_name(abstract_name: str, name_map: dict[str, str]) -> str:
    """Translate an abstract interface name to a real name.

    Direct hits are returned immediately (``iface1`` → ``eth0``).
    Otherwise, if the name starts with a mapped prefix followed by a dot
    the prefix is replaced (``iface1.42`` → ``eth0.42``).
    Names without a mapping are returned unchanged.
    """
    if abstract_name in name_map:
        return name_map[abstract_name]

    # Longest-prefix match so that e.g. "iface10" is not confused with
    # "iface1".
    for abstract, real in sorted(
        name_map.items(), key=lambda x: len(x[0]), reverse=True
    ):
        prefix = abstract + "."
        if abstract_name.startswith(prefix):
            return real + abstract_name[len(abstract):]

    return abstract_name


# ---------------------------------------------------------------------------
# Hardware helpers
# ---------------------------------------------------------------------------

def _find_hardware_for_mac(hardware: dict, mac: str):
    """Return ``(port_info, card_info)`` for the hardware port with *mac*.

    Returns ``(None, None)`` when no match is found.
    """
    for card in hardware.get("network_cards", []):
        for port in card.get("ports", []):
            if port.get("mac_address") == mac:
                return port, card
    return None, None


def _trace_mac(
    iface_name: str,
    deploy_config: dict,
    parent_of: dict[str, list[str]],
    port_config: dict,
    hardware: dict,
) -> str:
    """Walk up the parent chain until a physical interface is found and return its MAC."""
    visited: set[str] = set()
    current = iface_name

    while current and current not in visited:
        visited.add(current)
        iface_data = deploy_config.get(current, {})
        iface_type = iface_data.get("type", "physical")

        # An explicit mac_address on the interface itself wins.
        mac = iface_data.get("mac_address")
        if mac:
            return mac

        if iface_type == "physical":
            pc = port_config.get(current, {})
            return pc.get("mac_address") or pc.get("hw_mac_address", "")

        parents = parent_of.get(current, [])
        if parents:
            current = parents[0]
        else:
            break

    return ""


# ---------------------------------------------------------------------------
# VLAN-ID helpers
# ---------------------------------------------------------------------------

def _get_vid(
    iface_name: str,
    deploy_config: dict,
    parent_of: dict[str, list[str]],
) -> int:
    """Return the effective 802.1Q VID for *iface_name*.

    - physical / bond → 0 (untagged)
    - vlan            → its ``vid`` field
    - bridge          → inherited from its parent
    """
    iface_data = deploy_config.get(iface_name, {})
    iface_type = iface_data.get("type", "physical")

    if iface_type == "vlan":
        return iface_data.get("vid", 0)

    if iface_type == "bridge":
        parents = parent_of.get(iface_name, [])
        if parents:
            return _get_vid(parents[0], deploy_config, parent_of)

    return 0


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------

def _topological_sort(
    deploy_config: dict,
    parent_of: dict[str, list[str]],
) -> list[str]:
    """Return interface names ordered so that every parent appears before its children."""
    ordered: list[str] = []
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited or name not in deploy_config:
            return
        visited.add(name)
        for parent in parent_of.get(name, []):
            visit(parent)
        ordered.append(name)

    for name in deploy_config:
        visit(name)

    return ordered


# ---------------------------------------------------------------------------
# Parent / child relationship builder
# ---------------------------------------------------------------------------

def _build_relationships(
    deploy_config: dict,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return ``(parent_of, children_of)`` dicts derived from *deploy_config*.

    ``parent_of[name]``  — list of parent interface names
    ``children_of[name]`` — list of child interface names
    """
    parent_of: dict[str, list[str]] = {name: [] for name in deploy_config}
    children_of: dict[str, list[str]] = {name: [] for name in deploy_config}

    for iface_name, iface_data in deploy_config.items():
        iface_type = iface_data.get("type", "physical")

        if iface_type == "vlan":
            link = iface_data.get("link")
            if link:
                parent_of[iface_name] = [link]
                if link in children_of:
                    children_of[link].append(iface_name)

        elif iface_type == "bond":
            members = iface_data.get("members", [])
            parent_of[iface_name] = list(members)
            for member in members:
                if member in children_of:
                    children_of[member].append(iface_name)

        elif iface_type == "bridge":
            link = iface_data.get("link")
            if link:
                parent_of[iface_name] = [link]
                if link in children_of:
                    children_of[link].append(iface_name)

    return parent_of, children_of


# ---------------------------------------------------------------------------
# Per-machine conversion
# ---------------------------------------------------------------------------

def _convert_machine(machine_data: dict) -> dict:
    """Convert a single machine from the new format to the old format."""
    hardware = machine_data.get("hardware", {})
    port_config = machine_data.get("port-config", {})
    deploy_config = machine_data.get("default-deploy-config", {})

    if not deploy_config:
        return {"network": {"interfaces": []}}

    name_map = _build_name_map(port_config)
    vid_fabric_map = _build_vid_fabric_map(port_config)
    parent_of, children_of = _build_relationships(deploy_config)

    # Assign a sequential ``vlan.id`` for every unique VID used by the machine.
    all_vids: set[int] = set()
    for iface_name in deploy_config:
        all_vids.add(_get_vid(iface_name, deploy_config, parent_of))
    vlan_id_map = {vid: idx for idx, vid in enumerate(sorted(all_vids), start=1)}

    ordered = _topological_sort(deploy_config, parent_of)

    interfaces: list[dict] = []
    for iface_name in ordered:
        iface_data = deploy_config[iface_name]
        old_iface = _convert_interface(
            iface_name,
            iface_data,
            hardware,
            port_config,
            deploy_config,
            parent_of,
            children_of,
            vlan_id_map,
            name_map,
            vid_fabric_map,
        )
        interfaces.append(old_iface)

    return {"network": {"interfaces": interfaces}}


def _convert_interface(
    iface_name: str,
    iface_data: dict,
    hardware: dict,
    port_config: dict,
    deploy_config: dict,
    parent_of: dict[str, list[str]],
    children_of: dict[str, list[str]],
    vlan_id_map: dict[int, int],
    name_map: dict[str, str],
    vid_fabric_map: dict[int, int],
) -> dict:
    """Build a single old-format interface dict."""
    iface_type = iface_data.get("type", "physical")

    if iface_type == "physical":
        pc = port_config.get(iface_name, {})
        mac = pc.get("mac_address") or pc.get("hw_mac_address", "")
        port_hw, card_hw = _find_hardware_for_mac(hardware, mac)

        vendor = card_hw.get("vendor") if card_hw else None
        product = card_hw.get("product") if card_hw else None
        numa_node = 0
        link_connected = port_hw.get("link_detected", False) if port_hw else False
        link_speed = port_hw.get("link_speed", 0) if port_hw else 0
        interface_speed = port_hw.get("max_speed", 0) if port_hw else 0
    else:
        mac = _trace_mac(iface_name, deploy_config, parent_of, port_config, hardware)
        vendor = None
        product = None
        numa_node = None
        link_connected = False
        link_speed = 0
        interface_speed = 0

    vid = _get_vid(iface_name, deploy_config, parent_of)
    vlan_id = vlan_id_map.get(vid, 1)

    real_name = _translate_name(iface_name, name_map)

    old_iface: dict = {
        "name": real_name,
        "mac": mac,
        "link_connected": link_connected,
        "link_speed": link_speed,
        "interface_speed": interface_speed,
        "vendor": vendor,
        "product": product,
        "numa_node": numa_node,
        "type": iface_type,
    }

    # Bond-specific parameters.
    if iface_type == "bond":
        params = iface_data.get("params")
        if params:
            old_iface["params"] = dict(params)

    old_iface["vlan"] = {
        "id": vlan_id,
        "vid": vid,
        "mtu": iface_data.get("mtu", 1500),
        "fabric": vid_fabric_map.get(vid, 1),
    }

    old_iface["children"] = [
        _translate_name(c, name_map) for c in children_of.get(iface_name, [])
    ]
    old_iface["parents"] = [
        _translate_name(p, name_map) for p in parent_of.get(iface_name, [])
    ]
    old_iface["links"] = list(iface_data.get("links", []))

    return old_iface


if __name__ == "__main__":
    main()
