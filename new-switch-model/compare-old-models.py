#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pyyaml",
# ]
# ///
"""Compare machine network model definitions between two YAML files.

Usage:
    uv run compare-old-models.py <file1.yaml> <file2.yaml> [-m machine-name]

If a machine name is given, only that machine is compared.
If no machine name is given, all machines present in either file are compared.
"""

import argparse
import sys
from pathlib import Path

import yaml

RESET = "\033[0m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
BOLD = "\033[1m"


def load_yaml(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        sys.exit(2)
    with open(p) as f:
        return yaml.safe_load(f)


def index_interfaces(interfaces: list[dict]) -> dict[str, dict]:
    """Build a dict mapping interface name -> interface definition."""
    result = {}
    for iface in interfaces:
        name = iface.get("name")
        if name is not None:
            result[name] = iface
    return result


def compare_values(path: str, val1, val2, label1: str, label2: str) -> list[str]:
    """Recursively compare two values and return a list of difference descriptions."""
    diffs: list[str] = []

    if type(val1) is not type(val2):
        diffs.append(
            f"  {YELLOW}{path}{RESET}: type differs\n"
            f"    {RED}{label1}: {val1!r} ({type(val1).__name__}){RESET}\n"
            f"    {GREEN}{label2}: {val2!r} ({type(val2).__name__}){RESET}"
        )
        return diffs

    if isinstance(val1, dict):
        all_keys = sorted(set(list(val1.keys()) + list(val2.keys())))
        for key in all_keys:
            child_path = f"{path}.{key}"
            if key not in val1:
                diffs.append(
                    f"  {YELLOW}{child_path}{RESET}: only in {label2}\n"
                    f"    {GREEN}{label2}: {val2[key]!r}{RESET}"
                )
            elif key not in val2:
                diffs.append(
                    f"  {YELLOW}{child_path}{RESET}: only in {label1}\n"
                    f"    {RED}{label1}: {val1[key]!r}{RESET}"
                )
            else:
                diffs.extend(
                    compare_values(child_path, val1[key], val2[key], label1, label2)
                )
        return diffs

    if isinstance(val1, list):
        if val1 != val2:
            # For lists of dicts with a "name" key, compare element-wise by name
            if (
                val1
                and val2
                and isinstance(val1[0], dict)
                and isinstance(val2[0], dict)
                and "name" in val1[0]
                and "name" in val2[0]
            ):
                idx1 = {item["name"]: item for item in val1}
                idx2 = {item["name"]: item for item in val2}
                all_names = sorted(set(list(idx1.keys()) + list(idx2.keys())))
                for name in all_names:
                    child_path = f"{path}[name={name}]"
                    if name not in idx1:
                        diffs.append(
                            f"  {YELLOW}{child_path}{RESET}: only in {label2}\n"
                            f"    {GREEN}{label2}: {idx2[name]!r}{RESET}"
                        )
                    elif name not in idx2:
                        diffs.append(
                            f"  {YELLOW}{child_path}{RESET}: only in {label1}\n"
                            f"    {RED}{label1}: {idx1[name]!r}{RESET}"
                        )
                    else:
                        diffs.extend(
                            compare_values(
                                child_path, idx1[name], idx2[name], label1, label2
                            )
                        )
            else:
                # Compare lists element-by-element
                max_len = max(len(val1), len(val2))
                for i in range(max_len):
                    child_path = f"{path}[{i}]"
                    if i >= len(val1):
                        diffs.append(
                            f"  {YELLOW}{child_path}{RESET}: only in {label2}\n"
                            f"    {GREEN}{label2}: {val2[i]!r}{RESET}"
                        )
                    elif i >= len(val2):
                        diffs.append(
                            f"  {YELLOW}{child_path}{RESET}: only in {label1}\n"
                            f"    {RED}{label1}: {val1[i]!r}{RESET}"
                        )
                    else:
                        diffs.extend(
                            compare_values(
                                child_path, val1[i], val2[i], label1, label2
                            )
                        )
        return diffs

    # Scalar comparison
    if val1 != val2:
        diffs.append(
            f"  {YELLOW}{path}{RESET}:\n"
            f"    {RED}{label1}: {val1!r}{RESET}\n"
            f"    {GREEN}{label2}: {val2!r}{RESET}"
        )

    return diffs


def compare_interface(
    iface_name: str,
    iface1: dict,
    iface2: dict,
    label1: str,
    label2: str,
) -> list[str]:
    """Compare two interface definitions and return difference descriptions."""
    return compare_values(iface_name, iface1, iface2, label1, label2)


def compare_machine(
    machine_name: str,
    machine1: dict,
    machine2: dict,
    label1: str,
    label2: str,
) -> list[str]:
    """Compare two machine definitions and return difference descriptions."""
    diffs: list[str] = []

    network1 = machine1.get("network", {})
    network2 = machine2.get("network", {})

    interfaces1 = network1.get("interfaces", [])
    interfaces2 = network2.get("interfaces", [])

    idx1 = index_interfaces(interfaces1)
    idx2 = index_interfaces(interfaces2)

    all_iface_names = sorted(set(list(idx1.keys()) + list(idx2.keys())))

    for iface_name in all_iface_names:
        if iface_name not in idx1:
            diffs.append(
                f"  {YELLOW}interface {iface_name}{RESET}: "
                f"only in {GREEN}{label2}{RESET}"
            )
        elif iface_name not in idx2:
            diffs.append(
                f"  {YELLOW}interface {iface_name}{RESET}: "
                f"only in {RED}{label1}{RESET}"
            )
        else:
            iface_diffs = compare_interface(
                iface_name, idx1[iface_name], idx2[iface_name], label1, label2
            )
            diffs.extend(iface_diffs)

    # Compare any non-interface keys under the machine definition
    all_top_keys = sorted(set(list(machine1.keys()) + list(machine2.keys())))
    for key in all_top_keys:
        if key == "network":
            # Compare non-interfaces parts of network
            net_keys = sorted(
                set(list(network1.keys()) + list(network2.keys())) - {"interfaces"}
            )
            for nk in net_keys:
                nk_path = f"network.{nk}"
                if nk not in network1:
                    diffs.append(
                        f"  {YELLOW}{nk_path}{RESET}: only in {label2}\n"
                        f"    {GREEN}{label2}: {network2[nk]!r}{RESET}"
                    )
                elif nk not in network2:
                    diffs.append(
                        f"  {YELLOW}{nk_path}{RESET}: only in {label1}\n"
                        f"    {RED}{label1}: {network1[nk]!r}{RESET}"
                    )
                else:
                    diffs.extend(
                        compare_values(
                            nk_path, network1[nk], network2[nk], label1, label2
                        )
                    )
            continue
        if key not in machine1:
            diffs.append(
                f"  {YELLOW}{key}{RESET}: only in {label2}\n"
                f"    {GREEN}{label2}: {machine2[key]!r}{RESET}"
            )
        elif key not in machine2:
            diffs.append(
                f"  {YELLOW}{key}{RESET}: only in {label1}\n"
                f"    {RED}{label1}: {machine1[key]!r}{RESET}"
            )
        else:
            diffs.extend(compare_values(key, machine1[key], machine2[key], label1, label2))

    return diffs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare machine network model definitions between two YAML files."
    )
    parser.add_argument("file1", help="Path to the first YAML model file")
    parser.add_argument("file2", help="Path to the second YAML model file")
    parser.add_argument(
        "--machine",
        "-m",
        default=None,
        help="Name of a specific machine to compare. If omitted, all machines are compared.",
    )
    args = parser.parse_args()

    data1 = load_yaml(args.file1)
    data2 = load_yaml(args.file2)

    label1 = Path(args.file1).name
    label2 = Path(args.file2).name

    machines1 = data1.get("machines", {})
    machines2 = data2.get("machines", {})

    if args.machine:
        if args.machine not in machines1 and args.machine not in machines2:
            print(
                f"Error: machine '{args.machine}' not found in either file.",
                file=sys.stderr,
            )
            sys.exit(2)
        machine_names = [args.machine]
    else:
        machine_names = sorted(set(list(machines1.keys()) + list(machines2.keys())))

    if not machine_names:
        print("No machines found in either file.")
        return

    found_any_diff = False

    for machine_name in machine_names:
        if machine_name not in machines1:
            print(
                f"\n{BOLD}{CYAN}Machine: {machine_name}{RESET}\n"
                f"  {YELLOW}Only exists in {GREEN}{label2}{RESET}"
            )
            found_any_diff = True
            continue

        if machine_name not in machines2:
            print(
                f"\n{BOLD}{CYAN}Machine: {machine_name}{RESET}\n"
                f"  {YELLOW}Only exists in {RED}{label1}{RESET}"
            )
            found_any_diff = True
            continue

        diffs = compare_machine(
            machine_name, machines1[machine_name], machines2[machine_name], label1, label2
        )

        if diffs:
            found_any_diff = True
            print(f"\n{BOLD}{CYAN}Machine: {machine_name}{RESET}")
            for diff in diffs:
                print(diff)
        else:
            print(f"\n{BOLD}{CYAN}Machine: {machine_name}{RESET}  {GREEN}✓ identical{RESET}")

    if found_any_diff:
        sys.exit(1)
    else:
        print(f"\n{GREEN}{BOLD}All machines match.{RESET}")


if __name__ == "__main__":
    main()
