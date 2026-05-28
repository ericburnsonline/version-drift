#!/usr/bin/env python3
"""
version_drift.py — Quantify and visualize how a repository evolved across versions.

Usage:
  python version_drift.py scan   <root_dir>            # Scan all version subdirs
  python version_drift.py show   <root_dir>            # Print overview table
  python version_drift.py compare <root_dir> <vA> <vB> # Compare two versions
  python version_drift.py full   <root_dir>            # Run all comparisons
"""

import os
import sys
import csv
import json
import re
import textwrap
import urllib.request
import urllib.error
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd

# ── colours ──────────────────────────────────────────────────────────────────
C_BLUE   = "#378ADD"
C_TEAL   = "#1D9E75"
C_AMBER  = "#EF9F27"
C_CORAL  = "#D85A30"
C_GREEN  = "#639922"
C_GRAY   = "#888780"
C_RED    = "#E24B4A"
C_LIGHT  = "#E6F1FB"

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.alpha": 0.3,
    "figure.facecolor": "white",
    "axes.facecolor": "#FAFAFA",
})

# ── file extensions considered "code" ────────────────────────────────────────
CODE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp", ".h",
    ".go", ".rb", ".php", ".cs", ".swift", ".kt", ".rs", ".sh", ".bash",
    ".sql", ".r", ".scala", ".lua", ".pl", ".ex", ".exs",
}

SQL_EXTS = {".sql"}

# ── helpers ───────────────────────────────────────────────────────────────────

def count_lines(path: Path) -> int:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def scan_version_dir(ver_path: Path) -> dict:
    """Walk a single version directory and collect file metrics."""
    files = []
    for fp in sorted(ver_path.rglob("*")):
        if fp.is_file():
            ext = fp.suffix.lower()
            rel = str(fp.relative_to(ver_path))
            lines = count_lines(fp)
            files.append({
                "path": rel,
                "ext": ext,
                "lines": lines,
                "is_code": ext in CODE_EXTS,
                "is_sql": ext in SQL_EXTS,
            })
    return files


def parse_sql_schema(ver_path: Path) -> dict:
    """
    Extract table->columns mapping from any .sql files in the version dir.
    Handles CREATE TABLE statements.
    """
    tables = {}
    for fp in sorted(ver_path.rglob("*.sql")):
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        # find CREATE TABLE blocks
        for m in re.finditer(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"]?(\w+)[`\"]?\s*\(([^;]+?)\)",
            text, re.IGNORECASE | re.DOTALL
        ):
            tname = m.group(1).lower()
            body  = m.group(2)
            cols  = []
            for line in body.splitlines():
                line = line.strip().rstrip(",")
                if not line:
                    continue
                # skip constraints / indexes
                if re.match(r"(PRIMARY\s+KEY|UNIQUE|INDEX|KEY|FOREIGN\s+KEY|CONSTRAINT)", line, re.I):
                    continue
                col_name = re.split(r"\s+", line)[0].strip("`\"'")
                if col_name:
                    cols.append(col_name.lower())
            if cols:
                tables[tname] = cols
    return tables


def schema_delta(prev: dict, curr: dict) -> dict:
    """Compute a structured diff between two schema dicts."""
    prev_t = set(prev.keys())
    curr_t = set(curr.keys())
    added_tables   = sorted(curr_t - prev_t)
    removed_tables = sorted(prev_t - curr_t)
    modified = {}
    for t in prev_t & curr_t:
        pc, cc = set(prev[t]), set(curr[t])
        added_cols   = sorted(cc - pc)
        removed_cols = sorted(pc - cc)
        if added_cols or removed_cols:
            modified[t] = {"added": added_cols, "removed": removed_cols}
    score = (
        len(added_tables)   * 3
        + len(removed_tables) * 4
        + sum(len(v["added"]) for v in modified.values())
        + sum(len(v["removed"]) * 2 for v in modified.values())
    )
    return {
        "added_tables":   added_tables,
        "removed_tables": removed_tables,
        "modified":       modified,
        "score":          score,
    }


def schema_delta_text(delta: dict) -> str:
    lines = []
    for t in delta["added_tables"]:
        lines.append(f"  + TABLE {t} (new)")
    for t in delta["removed_tables"]:
        lines.append(f"  - TABLE {t} (dropped)")
    for t, info in delta["modified"].items():
        lines.append(f"  ~ TABLE {t}")
        for c in info["added"]:
            lines.append(f"      + {c}")
        for c in info["removed"]:
            lines.append(f"      - {c}")
    return "\n".join(lines) if lines else "  (no schema changes detected)"


# ── version discovery ─────────────────────────────────────────────────────────

def discover_versions(root: Path) -> list[dict]:
    """
    Find subdirectories whose names contain a version number.
    Supports: 1.0  v1.0  1_0  release-2.3  version_4 etc.
    Returns list sorted by version tuple.
    """
    entries = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        m = re.search(r"(\d+)[._-](\d+)(?:[._-](\d+))?", d.name)
        if not m:
            m2 = re.search(r"(\d+)$", d.name)
            if m2:
                tup = (int(m2.group(1)), 0, 0)
                label = m2.group(1)
            else:
                continue
        else:
            parts = [int(x) if x else 0 for x in m.groups()]
            tup   = tuple(parts)
            label = ".".join(str(p) for p in parts if p is not None)
            if m.lastindex < 3 or m.group(3) is None:
                label = f"{parts[0]}.{parts[1]}"
        entries.append({"dir": d, "version": label, "sort_key": tup})
    return sorted(entries, key=lambda e: e["sort_key"])


# ── AI narrative ──────────────────────────────────────────────────────────────

def ai_narrative(prompt: str, api_key: str | None) -> str:
    if not api_key:
        return "(Set ANTHROPIC_API_KEY environment variable to enable AI narratives.)"
    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 600,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type":      "application/json",
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
            return "".join(b.get("text", "") for b in data.get("content", []))
    except urllib.error.HTTPError as e:
        return f"(AI narrative failed: HTTP {e.code})"
    except Exception as e:
        return f"(AI narrative failed: {e})"


# ── chart helpers ─────────────────────────────────────────────────────────────

def save_fig(fig, out_path: Path):
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  → saved {out_path.name}")


def chart_loc_over_time(versions_data: list, out_path: Path):
    labels = [v["version"] for v in versions_data]
    locs   = [v["total_lines"] for v in versions_data]
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(labels, locs, marker="o", color=C_BLUE, linewidth=2.5, markersize=7)
    ax.fill_between(labels, locs, alpha=0.12, color=C_BLUE)
    for i, (lbl, val) in enumerate(zip(labels, locs)):
        ax.annotate(f"{val:,}", (lbl, val), textcoords="offset points",
                    xytext=(0, 10), ha="center", fontsize=9, color=C_BLUE)
    ax.set_title("Lines of code over versions", fontsize=13, pad=12)
    ax.set_ylabel("Lines of code")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{int(x):,}"))
    fig.tight_layout()
    save_fig(fig, out_path)


def chart_file_count_over_time(versions_data: list, out_path: Path):
    labels = [v["version"] for v in versions_data]
    counts = [v["file_count"] for v in versions_data]
    fig, ax = plt.subplots(figsize=(9, 4))
    bars = ax.bar(labels, counts, color=C_TEAL, width=0.55, zorder=3)
    for bar, val in zip(bars, counts):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                str(val), ha="center", va="bottom", fontsize=9)
    ax.set_title("File count over versions", fontsize=13, pad=12)
    ax.set_ylabel("Number of files")
    fig.tight_layout()
    save_fig(fig, out_path)


def chart_ext_breakdown(versions_data: list, out_path: Path):
    all_exts = set()
    for v in versions_data:
        all_exts |= set(v["by_ext"].keys())
    top_exts = sorted(all_exts, key=lambda e: sum(
        v["by_ext"].get(e, {}).get("lines", 0) for v in versions_data), reverse=True)[:6]
    labels = [v["version"] for v in versions_data]
    palette = [C_BLUE, C_TEAL, C_AMBER, C_CORAL, C_GREEN, C_GRAY]
    fig, ax = plt.subplots(figsize=(9, 4))
    bottom = [0] * len(versions_data)
    for i, ext in enumerate(top_exts):
        vals = [v["by_ext"].get(ext, {}).get("lines", 0) for v in versions_data]
        ax.bar(labels, vals, bottom=bottom, label=ext or "(no ext)",
               color=palette[i % len(palette)], width=0.55, zorder=3)
        bottom = [b + val for b, val in zip(bottom, vals)]
    ax.set_title("Lines of code by file type", fontsize=13, pad=12)
    ax.set_ylabel("Lines of code")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{int(x):,}"))
    ax.legend(fontsize=9, loc="upper left")
    fig.tight_layout()
    save_fig(fig, out_path)


def chart_schema_scores(versions_data: list, out_path: Path):
    if all(v["schema_score"] == 0 for v in versions_data[1:]):
        return  # nothing to show
    labels = [v["version"] for v in versions_data[1:]]
    scores = [v["schema_score"] for v in versions_data[1:]]
    fig, ax = plt.subplots(figsize=(9, 3.5))
    colors = [C_AMBER if s > 5 else C_TEAL for s in scores]
    bars = ax.bar(labels, scores, color=colors, width=0.55, zorder=3)
    for bar, val in zip(bars, scores):
        if val:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.1,
                    str(val), ha="center", va="bottom", fontsize=9)
    ax.set_title("Schema change score per version", fontsize=13, pad=12)
    ax.set_ylabel("Weighted change score")
    fig.tight_layout()
    save_fig(fig, out_path)


def chart_comparison(v_from: dict, v_to: dict, out_path: Path):
    """Side-by-side file-level comparison for two versions."""
    from_files = {f["path"]: f["lines"] for f in v_from["files"]}
    to_files   = {f["path"]: f["lines"] for f in v_to["files"]}
    shared = sorted(set(from_files) & set(to_files))

    # top 12 by combined size
    shared = sorted(shared, key=lambda p: from_files[p] + to_files[p], reverse=True)[:12]
    labels      = [os.path.basename(p) for p in shared]
    from_vals   = [from_files[p] for p in shared]
    to_vals     = [to_files[p] for p in shared]

    x = range(len(labels))
    w = 0.38
    fig, axes = plt.subplots(1, 2, figsize=(13, max(4, len(labels) * 0.45 + 2)))

    # left: absolute lines
    ax = axes[0]
    ax.barh([i + w/2 for i in x], from_vals, height=w, label=f"v{v_from['version']}", color=C_LIGHT, edgecolor=C_BLUE, linewidth=0.8)
    ax.barh([i - w/2 for i in x], to_vals,   height=w, label=f"v{v_to['version']}",   color=C_BLUE)
    ax.set_yticks(list(x))
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_title("File size (lines)", fontsize=11)
    ax.legend(fontsize=9)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{int(v):,}"))

    # right: % growth
    ax2 = axes[1]
    growths = []
    for fv, tv in zip(from_vals, to_vals):
        growths.append(round((tv - fv) / fv * 100, 1) if fv else 0)
    colors_g = [C_TEAL if g >= 0 else C_CORAL for g in growths]
    ax2.barh(list(x), growths, color=colors_g, height=0.6)
    ax2.axvline(0, color=C_GRAY, linewidth=0.8)
    ax2.set_yticks(list(x))
    ax2.set_yticklabels(labels, fontsize=9)
    ax2.set_title("Growth % per file", fontsize=11)
    ax2.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:+.0f}%"))

    fig.suptitle(f"v{v_from['version']} → v{v_to['version']} file comparison", fontsize=13, y=1.01)
    fig.tight_layout()
    save_fig(fig, out_path)


# ── CSV export ────────────────────────────────────────────────────────────────

def write_overview_csv(versions_data: list, out_path: Path):
    rows = []
    for i, v in enumerate(versions_data):
        prev = versions_data[i - 1] if i > 0 else None
        loc_delta_pct = (
            round((v["total_lines"] - prev["total_lines"]) / prev["total_lines"] * 100, 2)
            if prev and prev["total_lines"] else None
        )
        file_delta = (v["file_count"] - prev["file_count"]) if prev else None
        rows.append({
            "version":        v["version"],
            "file_count":     v["file_count"],
            "code_files":     v["code_files"],
            "total_lines":    v["total_lines"],
            "code_lines":     v["code_lines"],
            "loc_delta_pct":  loc_delta_pct,
            "file_delta":     file_delta,
            "schema_tables":  len(v["schema"]),
            "schema_score":   v["schema_score"],
        })
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"  → saved {out_path.name}")


def write_comparison_csv(v_from: dict, v_to: dict, out_path: Path):
    from_map = {f["path"]: f for f in v_from["files"]}
    to_map   = {f["path"]: f for f in v_to["files"]}
    all_paths = sorted(set(from_map) | set(to_map))
    rows = []
    for p in all_paths:
        f_lines = from_map[p]["lines"] if p in from_map else None
        t_lines = to_map[p]["lines"]   if p in to_map   else None
        status  = "unchanged"
        if p not in from_map:   status = "added"
        elif p not in to_map:   status = "removed"
        elif f_lines != t_lines: status = "modified"
        delta_pct = None
        if f_lines and t_lines:
            delta_pct = round((t_lines - f_lines) / f_lines * 100, 2)
        rows.append({
            "path":            p,
            "ext":             Path(p).suffix,
            "status":          status,
            f"lines_v{v_from['version']}": f_lines,
            f"lines_v{v_to['version']}":   t_lines,
            "delta_lines":     (t_lines or 0) - (f_lines or 0) if f_lines and t_lines else None,
            "delta_pct":       delta_pct,
        })
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"  → saved {out_path.name}")


# ── report text ───────────────────────────────────────────────────────────────

def write_report(content: str, out_path: Path):
    out_path.write_text(content, encoding="utf-8")
    print(f"  → saved {out_path.name}")


# ── core data loading ─────────────────────────────────────────────────────────

def load_all_versions(root: Path, verbose: bool = True) -> list[dict]:
    entries = discover_versions(root)
    if not entries:
        print(f"No versioned subdirectories found in {root}")
        sys.exit(1)
    versions_data = []
    for e in entries:
        if verbose:
            print(f"  scanning v{e['version']} ({e['dir'].name}) …")
        files = scan_version_dir(e["dir"])
        schema = parse_sql_schema(e["dir"])
        by_ext: dict = defaultdict(lambda: {"files": 0, "lines": 0})
        for f in files:
            by_ext[f["ext"]]["files"] += 1
            by_ext[f["ext"]]["lines"] += f["lines"]
        versions_data.append({
            "version":    e["version"],
            "dir":        e["dir"],
            "files":      files,
            "file_count": len(files),
            "code_files": sum(1 for f in files if f["is_code"]),
            "total_lines": sum(f["lines"] for f in files),
            "code_lines":  sum(f["lines"] for f in files if f["is_code"]),
            "by_ext":     dict(by_ext),
            "schema":     schema,
            "schema_score": 0,
        })
    # compute schema scores (vs previous)
    for i in range(1, len(versions_data)):
        delta = schema_delta(versions_data[i-1]["schema"], versions_data[i]["schema"])
        versions_data[i]["schema_score"] = delta["score"]
    return versions_data


# ── subcommands ───────────────────────────────────────────────────────────────

def cmd_show(root: Path, versions_data: list):
    """Print a rich overview table to stdout."""
    print(f"\n{'─'*70}")
    print(f"  Repository: {root.resolve()}")
    print(f"  Versions found: {len(versions_data)}")
    print(f"{'─'*70}")
    header = f"{'Version':>10} {'Files':>7} {'Code':>7} {'LoC':>9} {'LoC Δ%':>8} {'Tables':>7} {'SchemaΔ':>8}"
    print(header)
    print("─" * 70)
    for i, v in enumerate(versions_data):
        prev = versions_data[i - 1] if i > 0 else None
        loc_delta = (
            f"{(v['total_lines']-prev['total_lines'])/prev['total_lines']*100:+.1f}%"
            if prev and prev["total_lines"] else "  base"
        )
        schema_s = str(v["schema_score"]) if v["schema_score"] else "—"
        print(
            f"  v{v['version']:>8} {v['file_count']:>7} {v['code_files']:>7} "
            f"{v['total_lines']:>9,} {loc_delta:>8} "
            f"{len(v['schema']):>7} {schema_s:>8}"
        )
    print("─" * 70)


def cmd_compare(root: Path, versions_data: list, ver_a: str, ver_b: str,
                out_root: Path, api_key: str | None):
    """Compare two specific versions, saving all outputs under out_root/vA_to_vB/."""

    def find_ver(label):
        for v in versions_data:
            if v["version"] == label or v["version"].lstrip("v") == label.lstrip("v"):
                return v
        return None

    v_from = find_ver(ver_a)
    v_to   = find_ver(ver_b)
    if not v_from or not v_to:
        print(f"Error: could not find versions '{ver_a}' and/or '{ver_b}'.")
        print(f"Available: {[v['version'] for v in versions_data]}")
        sys.exit(1)

    tag     = f"v{v_from['version']}_to_v{v_to['version']}"
    out_dir = out_root / tag
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nComparing v{v_from['version']} → v{v_to['version']}")
    print(f"Output dir: {out_dir}\n")

    # ── metrics ──────────────────────────────────────────────────────────────
    from_files = {f["path"] for f in v_from["files"]}
    to_files   = {f["path"] for f in v_to["files"]}
    added_files   = sorted(to_files - from_files)
    removed_files = sorted(from_files - to_files)
    loc_delta_pct = (
        (v_to["total_lines"] - v_from["total_lines"]) / v_from["total_lines"] * 100
        if v_from["total_lines"] else 0
    )
    s_delta = schema_delta(v_from["schema"], v_to["schema"])

    # ── charts ────────────────────────────────────────────────────────────────
    chart_comparison(v_from, v_to, out_dir / "file_comparison.png")

    # ── CSV ───────────────────────────────────────────────────────────────────
    write_comparison_csv(v_from, v_to, out_dir / "file_changes.csv")

    # ── schema diff ───────────────────────────────────────────────────────────
    schema_text = schema_delta_text(s_delta)

    # ── AI narrative ──────────────────────────────────────────────────────────
    prompt = f"""You are analyzing a software repository. Write a clear, specific 4-6 sentence narrative 
describing what changed between v{v_from['version']} and v{v_to['version']}. 
Mention growth trends, notable new/removed files, and what the schema changes suggest about new features.
Start with "From v{v_from['version']} to v{v_to['version']},".

Metrics:
- LoC: {v_from['total_lines']:,} → {v_to['total_lines']:,} ({loc_delta_pct:+.1f}%)
- Files: {v_from['file_count']} → {v_to['file_count']} ({len(added_files):+d} added, {len(removed_files)} removed)
- Added files: {', '.join(added_files[:8]) or 'none'}
- Removed files: {', '.join(removed_files[:8]) or 'none'}
- Schema change score: {s_delta['score']}
- Schema diff:
{schema_text}"""

    print("Generating AI narrative …")
    narr = ai_narrative(prompt, api_key)

    # ── text report ───────────────────────────────────────────────────────────
    divider = "─" * 60
    report = f"""REPOSITORY COMPARISON REPORT
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}
{divider}
From:  v{v_from['version']}  ({v_from['dir'].name})
To:    v{v_to['version']}  ({v_to['dir'].name})
{divider}

METRICS
  Lines of code:  {v_from['total_lines']:>10,}  →  {v_to['total_lines']:>10,}  ({loc_delta_pct:+.1f}%)
  File count:     {v_from['file_count']:>10}  →  {v_to['file_count']:>10}
  Code files:     {v_from['code_files']:>10}  →  {v_to['code_files']:>10}
  DB tables:      {len(v_from['schema']):>10}  →  {len(v_to['schema']):>10}
  Schema Δ score: {s_delta['score']:>10}

FILES ADDED ({len(added_files)})
{chr(10).join('  + ' + f for f in added_files) or '  (none)'}

FILES REMOVED ({len(removed_files)})
{chr(10).join('  - ' + f for f in removed_files) or '  (none)'}

SCHEMA DIFF
{schema_text}

{divider}
AI NARRATIVE
{divider}
{textwrap.fill(narr, 72)}

{divider}
EXPORTS IN THIS DIRECTORY
  file_comparison.png  — side-by-side chart
  file_changes.csv     — per-file delta table
  report.txt           — this report
"""
    write_report(report, out_dir / "report.txt")

    # print summary to terminal
    print(f"\n{'─'*60}")
    print(f"  LoC {v_from['total_lines']:,} → {v_to['total_lines']:,}  ({loc_delta_pct:+.1f}%)")
    print(f"  Files added: {len(added_files)}   removed: {len(removed_files)}")
    print(f"  Schema Δ score: {s_delta['score']}")
    print(f"{'─'*60}")
    print(f"\nNARRATIVE\n{textwrap.fill(narr, 70)}\n")
    return out_dir


def cmd_overview(root: Path, versions_data: list, out_root: Path, api_key: str | None):
    """Generate full overview charts + CSV + narrative for all versions."""
    out_dir = out_root / "overview"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nGenerating overview → {out_dir}\n")

    chart_loc_over_time(versions_data, out_dir / "loc_over_time.png")
    chart_file_count_over_time(versions_data, out_dir / "file_count.png")
    chart_ext_breakdown(versions_data, out_dir / "ext_breakdown.png")
    chart_schema_scores(versions_data, out_dir / "schema_scores.png")

    write_overview_csv(versions_data, out_dir / "overview.csv")

    # AI narrative
    lines = []
    for i, v in enumerate(versions_data):
        prev = versions_data[i-1] if i > 0 else None
        delta_str = (
            f" ({(v['total_lines']-prev['total_lines'])/prev['total_lines']*100:+.1f}% LoC, "
            f"{v['file_count']-prev['file_count']:+d} files)"
            if prev and prev["total_lines"] else " (baseline)"
        )
        lines.append(f"v{v['version']}: {v['file_count']} files, {v['total_lines']:,} LoC{delta_str}")

    prompt = (
        "You are analyzing a software repository's evolution. "
        "Write a 4-6 sentence narrative describing the overall growth trajectory, "
        "development pace, and any notable inflection points. Be specific with numbers.\n\n"
        "Version data:\n" + "\n".join(lines)
    )
    print("Generating AI overview narrative …")
    narr = ai_narrative(prompt, api_key)

    report = f"""REPOSITORY OVERVIEW REPORT
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}
{'─'*60}
Versions: {', '.join('v'+v['version'] for v in versions_data)}
{'─'*60}

VERSION METRICS
{'Version':>10} {'Files':>7} {'LoC':>9} {'LoC Δ%':>8} {'Tables':>7} {'SchemaΔ':>8}
{'─'*60}
"""
    for i, v in enumerate(versions_data):
        prev = versions_data[i - 1] if i > 0 else None
        loc_d = (
            f"{(v['total_lines']-prev['total_lines'])/prev['total_lines']*100:+.1f}%"
            if prev and prev["total_lines"] else "  base"
        )
        report += (
            f"  v{v['version']:>8} {v['file_count']:>7} {v['total_lines']:>9,} "
            f"{loc_d:>8} {len(v['schema']):>7} {v['schema_score']:>8}\n"
        )
    report += f"\n{'─'*60}\nAI NARRATIVE\n{'─'*60}\n{textwrap.fill(narr, 72)}\n"
    report += f"\n{'─'*60}\nCHARTS\n  loc_over_time.png, file_count.png, ext_breakdown.png, schema_scores.png\nCSV\n  overview.csv\n"
    write_report(report, out_dir / "report.txt")
    print(f"\nNARRATIVE\n{textwrap.fill(narr, 70)}\n")
    return out_dir


def cmd_full(root: Path, versions_data: list, out_root: Path, api_key: str | None):
    """Run overview + all consecutive pairwise comparisons."""
    cmd_overview(root, versions_data, out_root, api_key)
    for i in range(len(versions_data) - 1):
        cmd_compare(
            root, versions_data,
            versions_data[i]["version"], versions_data[i+1]["version"],
            out_root, api_key,
        )
    print(f"\nAll outputs written to: {out_root.resolve()}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="version_drift",
        description="Quantify and visualize repository evolution across versions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              python version_drift.py scan    ./my_project
              python version_drift.py show    ./my_project
              python version_drift.py compare ./my_project 1.0 3.0
              python version_drift.py full    ./my_project
              python version_drift.py compare ./my_project 3.0 5.0 --out ./reports
        """),
    )
    parser.add_argument("command", choices=["scan", "show", "compare", "overview", "full"])
    parser.add_argument("root",    help="Root directory containing versioned subdirectories")
    parser.add_argument("from_ver", nargs="?", help="Start version (compare only)")
    parser.add_argument("to_ver",   nargs="?", help="End version (compare only)")
    parser.add_argument("--out",  default=None,
                        help="Output root directory (default: <root>/repo_analysis)")
    parser.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY"),
                        help="Anthropic API key (or set ANTHROPIC_API_KEY env var)")
    args = parser.parse_args()

    root     = Path(args.root).expanduser().resolve()
    out_root = Path(args.out).resolve() if args.out else root / "repo_analysis"

    if not root.is_dir():
        print(f"Error: '{root}' is not a directory.")
        sys.exit(1)

    if args.command == "scan":
        entries = discover_versions(root)

        # report any subdirs that were skipped
        all_dirs = [d for d in root.iterdir() if d.is_dir()]
        found_dirs = {e["dir"] for e in entries}
        skipped = [d for d in all_dirs if d not in found_dirs]

        if not entries:
            print(f"\nNo versioned directories found in {root}")
            print("\nversion_drift looks for a number in the directory name, e.g.:")
            print("  jobtracker_v1   release-2.0   v3   snapshot_4_1")
            if skipped:
                print(f"\nDirectories present but not matched ({len(skipped)}):")
                for d in sorted(skipped):
                    print(f"  {d.name}")
            return

        print(f"\nFound {len(entries)} versioned directories in {root}:\n")
        for e in entries:
            print(f"  v{e['version']:10s}  {e['dir'].name}")

        if skipped:
            print(f"\nSkipped (no version number detected):")
            for d in sorted(skipped):
                print(f"  {d.name}")

        if len(entries) >= 2:
            first = entries[0]["version"]
            last  = entries[-1]["version"]
            mid   = entries[len(entries) // 2]["version"]
            script = Path(sys.argv[0]).name
            print(f"\nSuggested next steps:")
            print(f"  python {script} show    .")
            print(f"  python {script} compare . {first} {last}   # first → last")
            if mid != first and mid != last:
                print(f"  python {script} compare . {first} {mid}   # first → midpoint")
            print(f"  python {script} full    .              # all consecutive comparisons")
        return

    print(f"Loading versions from {root} …")
    versions_data = load_all_versions(root, verbose=True)

    if args.command == "show":
        cmd_show(root, versions_data)

    elif args.command == "compare":
        if not args.from_ver or not args.to_ver:
            print("Error: 'compare' requires from_ver and to_ver arguments.")
            sys.exit(1)
        cmd_compare(root, versions_data, args.from_ver, args.to_ver, out_root, args.api_key)

    elif args.command == "overview":
        cmd_overview(root, versions_data, out_root, args.api_key)

    elif args.command == "full":
        cmd_full(root, versions_data, out_root, args.api_key)


if __name__ == "__main__":
    main()
