# Network Model Tools

## compare-old-models.py

A script that compares machine network model definitions between two YAML files and highlights any differences.

### Purpose

When working with network model YAML files (like `model-old.yaml`), this script helps identify changes between two versions of the model. It performs a deep comparison of machine definitions, comparing interfaces and their properties regardless of key ordering.

### Dependencies

The script uses [uv](https://docs.astral.sh/uv/) to manage dependencies. No manual installation is needed — just run the script with `uv run` and dependencies will be resolved automatically.

- **PyYAML** — for parsing YAML files

### Usage

```
# Compare all machines between two YAML files
uv run compare-old-models.py model-old.yaml model-other.yaml

# Compare a specific machine only
uv run compare-old-models.py model-old.yaml model-other.yaml --machine machine-simple
```

### Arguments

| Argument | Required | Description |
|----------|----------|-------------|
| `file1` | Yes | Path to the first YAML model file |
| `file2` | Yes | Path to the second YAML model file |
| `--machine` / `-m` | No | Name of a specific machine to compare. If omitted, all machines are compared. |

### Output

The script prints a summary for each machine, showing:

- Interfaces that exist in one file but not the other.
- Properties that differ between matching interfaces, with the values from each file displayed side by side.
- A confirmation when machines match exactly.

Exit codes:

- `0` — no differences found
- `1` — differences were found
- `2` — error (e.g. missing machine name, bad YAML)