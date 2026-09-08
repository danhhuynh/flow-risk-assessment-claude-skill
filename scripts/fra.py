#!/usr/bin/env python3
"""
fra.py — Flow Risk Assessment, Phase 1 (impact mapping)

Chỉ làm structural_only mode: flow nào bị chạm, canary có mù không,
manifest có rot không. Không tính xác suất — xem §15.4 của spec.

Dependency:  pip install pyyaml
Ngôn ngữ được phân tích: bất kỳ. Script này chỉ dùng git + glob.

Usage:
  fra.py init                            # scaffold .fra/
  fra.py doctor                          # kiểm manifest drift
  fra.py traffic --log access.log --format nginx --days 30
  fra.py analyze --base <sha> --head <sha>
  fra.py backtest --last 20              # chạy trên N PR/commit gần nhất
"""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("Cần pyyaml:  pip install pyyaml")

FRA_DIR = Path(".fra")
FLOWS_DIR = FRA_DIR / "flows"
CACHE_DIR = FRA_DIR / "cache"
CONFIG_PATH = FRA_DIR / "config.yaml"

DEFAULT_CONFIG = {
    "canary": {
        "duration_min": 30,
        "traffic_share": 0.05,
        "min_executions": 30,
    },
    "cochange": {
        "window_days": 365,
        "min_support": 4,
        "min_confidence": 0.30,
        "max_commit_files": 25,
    },
    "ignore_paths": [
        "*.md", "*.lock", "*.snap", "*.min.js", "*.min.css",
        "vendor/*", "node_modules/*", "dist/*", "build/*",
    ],
}


# ────────────────────────────── git ──────────────────────────────

def git(*args, check=True):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"git {' '.join(args)} thất bại:\n{r.stderr.strip()}")
    return r.stdout


def changed_files(base: str, head: str) -> list[str]:
    """Trả về đường dẫn MỚI (xử lý đúng rename/copy)."""
    out = git("diff", "--name-status", "-M", "-C", f"{base}...{head}")
    files = []
    for line in out.splitlines():
        parts = line.split("\t")
        if not parts or len(parts) < 2:
            continue
        status = parts[0]
        if status[0] in ("R", "C") and len(parts) >= 3:
            files.append(parts[2])
        elif status[0] != "D":
            files.append(parts[1])
        else:
            files.append(parts[1])  # deletion: vẫn cần biết đã xóa gì
    return files


def diff_stats(base: str, head: str) -> dict:
    out = git("diff", "--numstat", f"{base}...{head}")
    added = removed = 0
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) == 3 and p[0].isdigit() and p[1].isdigit():
            added += int(p[0])
            removed += int(p[1])
    return {"lines_added": added, "lines_removed": removed}


# ─────────────────── Tier 0: co-change coupling ───────────────────

def build_cochange(cfg) -> dict:
    """{file: {file: confidence}} — coupling thực nghiệm từ git history.

    Bắt được phụ thuộc mà static analysis không thấy:
    template↔controller, migration↔model, config↔code, IaC↔app.
    """
    c = cfg["cochange"]
    out = git("log", f"--since={c['window_days']} days ago",
              "--name-only", "--pretty=format:===COMMIT===")

    commits, cur = [], []
    for line in out.splitlines():
        if line.startswith("===COMMIT==="):
            if cur:
                commits.append(set(cur))
            cur = []
        elif line.strip():
            cur.append(line.strip())
    if cur:
        commits.append(set(cur))

    # Loại commit khổng lồ: 1 lần format toàn repo tạo coupling giả toàn tập
    before = len(commits)
    commits = [s for s in commits if 1 < len(s) <= c["max_commit_files"]]

    solo = Counter()
    pair = defaultdict(Counter)
    for files in commits:
        for f in files:
            solo[f] += 1
        for a in files:
            for b in files:
                if a != b:
                    pair[a][b] += 1

    coupling = {}
    for a, partners in pair.items():
        if solo[a] < c["min_support"]:
            continue
        linked = {
            b: round(n / solo[a], 3)
            for b, n in partners.items()
            if n >= c["min_support"] and n / solo[a] >= c["min_confidence"]
        }
        if linked:
            coupling[a] = linked

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commits_scanned": before,
        "commits_used": len(commits),
        "coupling": coupling,
    }


def load_cochange(cfg, refresh=False) -> dict:
    path = CACHE_DIR / "cochange.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())["coupling"]
    data = build_cochange(cfg)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    return data["coupling"]


# ───────────────────────── manifest ──────────────────────────────

def load_flows() -> list[dict]:
    if not FLOWS_DIR.exists():
        sys.exit(f"Không thấy {FLOWS_DIR}. Chạy: fra.py init")
    flows = []
    for p in sorted(FLOWS_DIR.glob("*.yaml")) + sorted(FLOWS_DIR.glob("*.yml")):
        d = yaml.safe_load(p.read_text())
        if not d:
            continue
        d["_path"] = str(p)
        flows.append(d)
    if not flows:
        sys.exit(f"{FLOWS_DIR} rỗng. Xem mẫu trong {FLOWS_DIR}/example.yaml")
    return flows


def flow_entities(flow: dict) -> list[str]:
    out = []
    for step in flow.get("steps", []):
        out.extend(step.get("entities", []))
    return out


def entity_matches(pattern: str, path: str) -> bool:
    """Khớp entity ref với đường dẫn file.

    Bỏ phần #func:/#L (thang L2/L3) — Phase 1 chỉ làm ở mức file (L1).
    """
    p = pattern.split("#", 1)[0].replace("**", "*")
    return fnmatch.fnmatch(path, p)


def ignored(path: str, cfg) -> bool:
    return any(fnmatch.fnmatch(path, g) for g in cfg["ignore_paths"])


# ───────────────────── canary blindness math ──────────────────────

def canary_check(epd, cfg) -> dict | None:
    """Canary chỉ cho tín hiệu nếu đủ số lần thực thi trong cửa sổ.

    Đây là phép chia đơn giản nhưng thường bị bỏ qua: flow tần suất thấp
    làm canary xanh 100% mà không kiểm chứng gì.
    """
    c = cfg["canary"]
    if epd is None:
        return {"status": "unknown", "message": "Không có dữ liệu traffic."}
    if epd <= 0:
        return {"status": "no_traffic", "message": "Đo được: không có traffic."}

    window_days = c["duration_min"] / 1440.0
    expected = epd * window_days * c["traffic_share"]

    if expected >= c["min_executions"]:
        return {
            "status": "ok",
            "expected_executions": round(expected, 1),
            "message": f"Canary cho tín hiệu (~{expected:.0f} lần thực thi).",
        }

    need_min = c["min_executions"] / (epd * c["traffic_share"]) * 1440

    # Chọn đơn vị theo độ lớn — "0.0 ngày" là output vô nghĩa
    if need_min < 90:
        need_s = f"{need_min:.0f} phút"
    elif need_min < 1440:
        need_s = f"{need_min/60:.1f} giờ"
    else:
        need_s = f"{need_min/1440:.1f} ngày"

    # Kéo dài cửa sổ chỉ thực tế khi dưới ~1 ngày
    if need_min <= 1440:
        remedy = f"Kéo cửa sổ canary lên ~{need_s}, hoặc tăng traffic_share."
    else:
        remedy = (
            f"Cần ~{need_s} để có tín hiệu — không thực tế. Dùng feature flag "
            f"+ shadow traffic hoặc synthetic probe thay canary."
        )

    return {
        "status": "blind",
        "expected_executions": round(expected, 2),
        "required_duration_min": round(need_min),
        "message": (
            f"CANARY MÙ: chỉ ~{expected:.2f} lần thực thi trong "
            f"{c['duration_min']} phút @ {c['traffic_share']:.0%} traffic "
            f"(cần ≥{c['min_executions']}). Canary sẽ PASS rồi lỗi ở full "
            f"rollout. {remedy}"
        ),
    }


# ───────────────────── severity amplifiers ────────────────────────

SEV_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def severity_note(sev: dict) -> list[str]:
    """Bốn cờ khuếch đại phân biệt rủi ro tốt hơn cả severity class."""
    notes = []
    if sev.get("idempotent") is False:
        notes.append("không idempotent (retry có thể gây double-effect)")
    if sev.get("reversible") is False:
        notes.append("không reversible")
    ca = sev.get("compensating_action")
    if ca == "manual":
        notes.append("khôi phục phải làm tay")
    elif ca == "none":
        notes.append("KHÔNG có compensating action")
    if sev.get("external_side_effects"):
        notes.append("có side effect ra hệ thống ngoài")
    return notes


# ─────────────────────────── analyze ──────────────────────────────

def analyze(base: str, head: str, cfg, use_cochange=True) -> dict:
    flows = load_flows()
    files = [f for f in changed_files(base, head) if not ignored(f, cfg)]

    coupling = load_cochange(cfg) if use_cochange else {}

    # Mở rộng closure Tier 0, giữ confidence riêng theo nguồn
    expanded: dict[str, tuple[str, float]] = {f: ("direct", 1.0) for f in files}
    for f in files:
        for partner, conf in coupling.get(f, {}).items():
            if partner not in expanded and not ignored(partner, cfg):
                expanded[partner] = ("cochange", conf)

    results = []
    for flow in flows:
        ents = flow_entities(flow)
        hits = []
        for path, (src, conf) in expanded.items():
            matched = [e for e in ents if entity_matches(e, path)]
            if matched:
                hits.append({
                    "file": path, "via": src,
                    "confidence": conf, "entities": matched,
                })
        if not hits:
            continue

        sev = flow.get("severity", {}) or {}
        op = flow.get("operational", {}) or {}
        epd = op.get("executions_per_day")

        direct = [h for h in hits if h["via"] == "direct"]
        results.append({
            "flow_id": flow.get("id", Path(flow["_path"]).stem),
            "flow_name": flow.get("name", ""),
            "severity_class": sev.get("class", "unknown"),
            "severity_notes": severity_note(sev),
            "executions_per_day": epd,
            "files_direct": [h["file"] for h in direct],
            "files_cochange": [h["file"] for h in hits if h["via"] == "cochange"],
            "reach_confidence": max((h["confidence"] for h in hits), default=0),
            "canary": canary_check(epd, cfg),
            "invariants": [
                {"id": iv.get("id"), "statement": iv.get("statement"),
                 "provenance": iv.get("provenance"), "step": step.get("id")}
                for step in flow.get("steps", [])
                for iv in step.get("invariants", [])
                if iv.get("status") == "confirmed"
                and any(entity_matches(e, h["file"])
                        for e in step.get("entities", []) for h in direct)
            ],
        })

    # Sắp theo severity, rồi theo có file trực tiếp, rồi theo traffic
    results.sort(key=lambda r: (
        -SEV_ORDER.get(r["severity_class"], 0),
        -len(r["files_direct"]),
        -(r["executions_per_day"] or 0),
    ))

    covered = {f for r in results for f in r["files_direct"]}
    return {
        "schema_version": 1,
        "mode": "structural_only",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base": base, "head": head,
        "diff": diff_stats(base, head) | {"files": len(files)},
        "flows_affected": results,
        "unmapped_files": sorted(set(files) - covered),
    }


# ─────────────────────────── doctor ───────────────────────────────

def doctor(cfg) -> int:
    flows = load_flows()
    tracked = set(git("ls-files").splitlines())
    errors = warnings = 0

    for flow in flows:
        fid = flow.get("id", Path(flow["_path"]).stem)

        for required in ("id", "name", "severity", "steps"):
            if required not in flow:
                print(f"  ✗ {fid}: thiếu field bắt buộc '{required}'")
                errors += 1

        sev = flow.get("severity", {}) or {}
        for flag in ("idempotent", "reversible", "compensating_action"):
            if flag not in sev:
                print(f"  ⚠ {fid}: thiếu severity.{flag} "
                      f"(cờ khuếch đại, ảnh hưởng xếp hạng)")
                warnings += 1

        # Drift: entity ref không resolve được = lỗi thật, không phải cảnh báo
        for step in flow.get("steps", []):
            for ent in step.get("entities", []):
                if not any(entity_matches(ent, f) for f in tracked):
                    print(f"  ✗ {fid}/{step.get('id')}: entity không resolve: {ent}")
                    errors += 1

        op = flow.get("operational", {}) or {}
        if op.get("executions_per_day") is None:
            print(f"  ⚠ {fid}: thiếu operational.executions_per_day "
                  f"(không tính được canary blindness)")
            warnings += 1

        if sev.get("class") == "critical":
            obs = op.get("observability", {}) or {}
            if not obs.get("alertable_metric"):
                print(f"  ⚠ {fid}: flow critical không có alertable_metric")
                warnings += 1

        n_conf = sum(
            1 for s in flow.get("steps", [])
            for iv in s.get("invariants", [])
            if iv.get("status") == "confirmed"
        )
        if n_conf == 0:
            print(f"  ⚠ {fid}: chưa có invariant nào status=confirmed")
            warnings += 1

    print(f"\n{len(flows)} flow · {errors} lỗi · {warnings} cảnh báo")
    return 1 if errors else 0


# ─────────────────────────── traffic ──────────────────────────────

LOG_FORMATS = {
    "nginx":      r'"(?P<method>[A-Z]+) (?P<path>[^ ?"]+)',
    "apache":     r'"(?P<method>[A-Z]+) (?P<path>[^ ?"]+)',
    "cloudfront": r'\t(?P<method>[A-Z]+)\t[^\t]*\t(?P<path>[^\t?]+)',
    "json":       r'"method"\s*:\s*"(?P<method>[A-Z]+)".*?"(?:path|uri)"\s*:\s*"(?P<path>[^"?]+)"',
}

NORMALIZERS = [
    (re.compile(r'/\d+(?=/|$)'), '/:id'),
    (re.compile(r'/[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}'), '/:uuid'),
    (re.compile(r'/[0-9a-fA-F]{24,}'), '/:hash'),
    (re.compile(r'\?.*$'), ''),
]


def normalize_path(p: str) -> str:
    for pat, rep in NORMALIZERS:
        p = pat.sub(rep, p)
    return p


def build_traffic(log_path: str, fmt: str, days: int) -> dict:
    pat = re.compile(LOG_FORMATS[fmt])
    counts = Counter()
    total = matched = 0
    with open(log_path, errors="replace") as fh:
        for line in fh:
            total += 1
            m = pat.search(line)
            if m:
                matched += 1
                counts[f"{m.group('method')} {normalize_path(m.group('path'))}"] += 1

    if total and matched / total < 0.5:
        print(f"⚠ Chỉ parse được {matched}/{total} dòng ({matched/total:.0%}). "
              f"Format có thể sai — kiểm lại --format.", file=sys.stderr)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "lines_total": total,
        "lines_matched": matched,
        "endpoints": {
            ep: round(n / days, 2)
            for ep, n in counts.most_common(300)
        },
    }


# ──────────────────────────── init ────────────────────────────────

EXAMPLE_FLOW = """\
schema_version: 1
id: example_checkout
name: "MÔ TẢ FLOW BẰNG NGÔN NGỮ NGHIỆP VỤ"
owners: ["@you"]

severity:
  class: critical            # critical | high | medium | low
  rationale: "Vì sao nó nghiêm trọng — mất tiền? hỏng dữ liệu? rò rỉ?"
  impact_types: [financial]
  # 4 cờ dưới đây phân biệt rủi ro tốt hơn cả 'class' ở trên
  idempotent: false
  reversible: false
  compensating_action: manual    # automatic | manual | none
  external_side_effects: true

operational:
  source: adapter:accesslog
  executions_per_day: 0      # ← LẤY TỪ LOG, đừng đoán. Xem: fra.py traffic
  observability:
    has_slo: false
    alertable_metric: ""

steps:
  - id: step_one
    name: "Tên bước"
    entities:
      # Mức file (L1) là đủ để bắt đầu. Wildcard extension để sống qua refactor.
      - "src/services/CheckoutService.*"
      - "src/controllers/**"
    invariants:
      - id: inv_example
        statement: "Điều gì PHẢI luôn đúng ở bước này"
        provenance: incident   # incident (tốt nhất) | human | llm_proposed
        incident_ref: "INC-xxxx"
        status: confirmed      # chỉ 'confirmed' được tính vào xếp hạng

entry_points:
  - kind: http
    pattern: "POST /api/checkout"

maintenance:
  last_verified: ""
  verified_by: ""
"""


def do_init():
    FLOWS_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False,
                                              allow_unicode=True))
        print(f"✓ {CONFIG_PATH}")

    ex = FLOWS_DIR / "example.yaml"
    if not ex.exists():
        ex.write_text(EXAMPLE_FLOW)
        print(f"✓ {ex}")

    gi = FRA_DIR / ".gitignore"
    if not gi.exists():
        gi.write_text("cache/\n")
        print(f"✓ {gi}")

    print("\nTiếp theo:")
    print("  1. fra.py traffic --log <access.log> --format nginx --days 30")
    print("  2. Sửa .fra/flows/example.yaml thành flow thật (copy ra 3 file)")
    print("  3. fra.py doctor")


# ─────────────────────────── report ───────────────────────────────

def print_report(rep: dict):
    d = rep["diff"]
    print(f"\nFRA · {d['files']} file · +{d['lines_added']}/-{d['lines_removed']} "
          f"· mode={rep['mode']}\n")

    if not rep["flows_affected"]:
        print("Không flow nào trong manifest bị chạm.")
        print("→ Kiểm tra: manifest có phủ vùng code này chưa? (unmapped_files)")
    for r in rep["flows_affected"]:
        icon = {"critical": "🔴", "high": "🟠", "medium": "🟡"}.get(
            r["severity_class"], "⚪")
        epd = r["executions_per_day"]
        epd_s = f"{epd:,.0f}/ngày" if epd else "traffic chưa biết"
        print(f"{icon} {r['flow_id']} — {r['flow_name']}")
        print(f"   {r['severity_class']} · {epd_s}")
        if r["severity_notes"]:
            print(f"   ⚑ {'; '.join(r['severity_notes'])}")
        if r["files_direct"]:
            print(f"   Chạm trực tiếp: {', '.join(r['files_direct'][:6])}"
                  + (" …" if len(r["files_direct"]) > 6 else ""))
        if r["files_cochange"]:
            print(f"   Coupling lịch sử: {', '.join(r['files_cochange'][:4])}"
                  + (" …" if len(r["files_cochange"]) > 4 else ""))
        c = r["canary"]
        if c and c["status"] == "blind":
            print(f"   ⚠️  {c['message']}")
        for iv in r["invariants"]:
            print(f"   📌 [{iv['provenance']}] {iv['id']}: {iv['statement']}")
        print()

    if rep["unmapped_files"]:
        n = len(rep["unmapped_files"])
        print(f"Không thuộc flow nào ({n}): "
              f"{', '.join(rep['unmapped_files'][:8])}"
              + (" …" if n > 8 else ""))


# ────────────────────────── backtest ──────────────────────────────

def backtest(n: int, cfg):
    """Chạy trên N merge commit gần nhất. Bước kiểm chứng rẻ nhất và
    hay bị bỏ: FRA có gắn cờ đúng những thay đổi đã gây sự cố không?"""
    shas = git("log", "--merges", "-n", str(n),
               "--pretty=format:%H|%s").splitlines()
    if not shas:
        shas = git("log", "-n", str(n), "--pretty=format:%H|%s").splitlines()

    for entry in shas:
        sha, subject = entry.split("|", 1)
        try:
            rep = analyze(f"{sha}^", sha, cfg, use_cochange=False)
        except SystemExit:
            continue
        flows = rep["flows_affected"]
        tag = ", ".join(f"{f['flow_id']}({f['severity_class']})"
                        for f in flows) or "—"
        print(f"{sha[:8]}  {len(flows)} flow  {tag:<48}  {subject[:50]}")


# ──────────────────────────── main ────────────────────────────────

def main():
    ap = argparse.ArgumentParser(prog="fra.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    sub.add_parser("doctor")

    a = sub.add_parser("analyze")
    a.add_argument("--base", required=True)
    a.add_argument("--head", default="HEAD")
    a.add_argument("--json", help="ghi report JSON ra file")
    a.add_argument("--no-cochange", action="store_true")
    a.add_argument("--refresh-cochange", action="store_true")

    t = sub.add_parser("traffic")
    t.add_argument("--log", required=True)
    t.add_argument("--format", choices=list(LOG_FORMATS), default="nginx")
    t.add_argument("--days", type=int, required=True)
    t.add_argument("--top", type=int, default=40)

    b = sub.add_parser("backtest")
    b.add_argument("--last", type=int, default=20)

    args = ap.parse_args()

    cfg = DEFAULT_CONFIG
    if CONFIG_PATH.exists():
        loaded = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        cfg = {**DEFAULT_CONFIG, **loaded}

    if args.cmd == "init":
        do_init()

    elif args.cmd == "doctor":
        sys.exit(doctor(cfg))

    elif args.cmd == "traffic":
        data = build_traffic(args.log, args.format, args.days)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        out = CACHE_DIR / "traffic.json"
        out.write_text(json.dumps(data, indent=2))
        print(f"✓ {out}  ({data['lines_matched']:,} dòng khớp)\n")
        print(f"{'lần/ngày':>12}  endpoint")
        for ep, rate in list(data["endpoints"].items())[:args.top]:
            print(f"{rate:>12,.1f}  {ep}")
        print("\n→ Dùng các số này cho operational.executions_per_day trong manifest.")

    elif args.cmd == "analyze":
        if args.refresh_cochange:
            load_cochange(cfg, refresh=True)
        rep = analyze(args.base, args.head, cfg,
                      use_cochange=not args.no_cochange)
        print_report(rep)
        if args.json:
            Path(args.json).write_text(json.dumps(rep, indent=2,
                                                  ensure_ascii=False))

    elif args.cmd == "backtest":
        backtest(args.last, cfg)


if __name__ == "__main__":
    main()
