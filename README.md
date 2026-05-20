# repo_analyzer

A command-line tool to quantify and visualize how a software repository evolved across versions. Point it at a directory of versioned subdirectories and it produces charts, CSVs, schema diffs, and (optionally) AI-written narratives describing what changed and why it matters.

> **Status: vibe coded, not yet formally tested.**
> This tool was built with AI assistance as part of an experiment in rapid development. It works on the demo dataset it was built against, but has not been tested across a wide range of real-world repositories. Use with appropriate skepticism, and please open an issue if something breaks.

---

## Background

This is one piece of a larger project — also vibe coded — where I'm using it to measure and understand the *amount* of change between successive versions of that project. Think of it as a retrospective lens on your own development velocity: how fast did the codebase grow? When did the schema stabilize? Which files absorbed the most churn?

---

## What it does

- Scans versioned subdirectories (`v1.0/`, `v2.0/`, `release-3.1/`, etc.) and extracts file counts, lines of code, and file type breakdown
- Parses SQL schema files to detect table and column additions, removals, and modifications
- Computes a weighted **schema change score** across versions
- Generates charts (LoC over time, file count, extension breakdown, schema delta scores, per-file comparison)
- Exports CSVs and plain-text reports per comparison
- Optionally calls the Anthropic API to write a plain-English narrative of what changed — "From v1 to v3 we saw..."
- Saves all outputs into named directories (`v1.0_to_v3.0/`, `overview/`, etc.)

---

## Requirements

```
pip install -r requirements.txt
```

Python 3.10+ is required (uses `str | None` union syntax).

---

## Usage

```bash
# Discover versioned subdirectories
python repo_analyzer.py scan ./my_project

# Print a summary table to the terminal
python repo_analyzer.py show ./my_project

# Generate overview charts + CSV across all versions
python repo_analyzer.py overview ./my_project

# Compare two specific versions — saves to v2.0_to_v4.0/
python repo_analyzer.py compare ./my_project 2.0 4.0

# Run everything: overview + all consecutive comparisons
python repo_analyzer.py full ./my_project

# Custom output location
python repo_analyzer.py compare ./my_project 1.0 5.0 --out ./reports
```

### Output structure

```
repo_analysis/
  overview/
    loc_over_time.png
    file_count.png
    ext_breakdown.png
    schema_scores.png
    overview.csv
    report.txt
  v1.0_to_v2.0/
    file_comparison.png
    file_changes.csv
    report.txt
  v2.0_to_v3.0/
    ...
```

---

## AI narratives

Set your Anthropic API key and the tool will generate plain-English summaries of each comparison. Without a key, everything else still works — the narrative section in reports will just show a placeholder.

**Windows (PowerShell, current session):**
```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

**Windows (permanent, user account):**
```powershell
[System.Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-...", "User")
```

**macOS / Linux:**
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Or pass it directly:
```bash
python repo_analyzer.py full ./my_project --api-key sk-ant-...
```

---

## Version directory detection

The tool auto-detects version numbers in directory names. All of these work:

| Directory name | Detected version |
|----------------|-----------------|
| `v1.0`         | 1.0             |
| `v2.3.1`       | 2.3.1           |
| `release-3.0`  | 3.0             |
| `version_4`    | 4               |
| `1_0`          | 1.0             |

---

## Schema detection

Parses `CREATE TABLE` statements from any `.sql` files found in each version directory. Handles quoted identifiers, `IF NOT EXISTS`, and skips constraint/index lines. Does not yet support migrations files or ORM model definitions — PRs welcome.

---

## License

MIT. See [LICENSE](LICENSE).

This is free to use, fork, and adapt. Attribution appreciated but not required.

---

## Contributing

Issues and PRs welcome, especially around:
- Supporting more SQL dialects and ORM formats
- Git-native mode (read directly from a repo's commit history rather than snapshot directories)
- More chart types
- Windows path edge cases
