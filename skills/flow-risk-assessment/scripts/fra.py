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

from __future__ import annotations

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

if sys.version_info < (3, 8):
    sys.exit(
        f"Cần Python ≥3.8, đang chạy {sys.version_info.major}.{sys.version_info.minor}.\n"
        "  macOS: brew install python@3.12  (rồi dùng python3.12 tools/fra.py)"
    )

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

# Field hợp lệ của một invariant. Viết sai tên field (ví dụ 'source' thay vì
# 'provenance' + 'incident_ref') khiến invariant KHÔNG được tính mà không báo lỗi.
INVARIANT_KEYS = {"id", "statement", "provenance", "incident_ref", "status", "note"}
INVARIANT_REQUIRED = {"id", "statement", "provenance", "status"}
PROVENANCE_VALUES = {"incident", "human", "llm_proposed"}
STATUS_VALUES = {"confirmed", "proposed", "deprecated"}


def check_invariant(iv, label: str, step_id: str) -> tuple[int, int]:
    """Trả về (errors, warnings). Bắt lỗi viết sai schema."""
    e = w = 0
    if not isinstance(iv, dict):
        print(f"  ✗ {label}/{step_id}: invariant không phải mapping: {iv!r}")
        return 1, 0

    unknown = set(iv) - INVARIANT_KEYS
    if unknown:
        hint = ""
        if "source" in unknown:
            hint = " → dùng 'provenance' + 'incident_ref' thay cho 'source'"
        elif "desc" in unknown or "description" in unknown:
            hint = " → dùng 'statement'"
        print(f"  ✗ {label}/{step_id}: invariant '{iv.get('id','?')}' có field "
              f"không hợp lệ: {sorted(unknown)}{hint}")
        e += 1

    missing = INVARIANT_REQUIRED - set(iv)
    if missing:
        print(f"  ✗ {label}/{step_id}: invariant '{iv.get('id','?')}' thiếu "
              f"field bắt buộc: {sorted(missing)} — sẽ KHÔNG được tính vào điểm")
        e += 1

    p = iv.get("provenance")
    if p is not None and p not in PROVENANCE_VALUES:
        print(f"  ✗ {label}/{step_id}: provenance '{p}' không hợp lệ "
              f"(phải là {sorted(PROVENANCE_VALUES)})")
        e += 1

    st = iv.get("status")
    if st is not None and st not in STATUS_VALUES:
        print(f"  ✗ {label}/{step_id}: status '{st}' không hợp lệ "
              f"(phải là {sorted(STATUS_VALUES)})")
        e += 1

    if p == "incident" and not iv.get("incident_ref"):
        print(f"  ⚠ {label}/{step_id}: provenance=incident nhưng thiếu "
              f"incident_ref (mất truy vết về sự cố gốc)")
        w += 1

    return e, w


def check_glob_breadth(pattern: str, tracked: set, label: str,
                       step_id: str) -> int:
    """Glob quá rộng khiến mọi PR chạm mọi flow. Trả về số cảnh báo."""
    p = pattern.split("#", 1)[0].replace("**", "*")
    n = sum(1 for f in tracked if fnmatch.fnmatch(f, p))
    total = max(len(tracked), 1)
    share = n / total
    if n > 150 or share > 0.10:
        print(f"  ⚠ {label}/{step_id}: entity '{pattern}' khớp {n} file "
              f"({share:.0%} repo) — glob quá rộng, mọi PR sẽ chạm flow này. "
              f"Thu hẹp về thư mục hoặc file cụ thể")
        return 1
    return 0


def doctor(cfg) -> int:
    flows = load_flows()
    tracked = set(git("ls-files").splitlines())
    errors = warnings = 0

    # Phát hiện id trùng hoặc chưa điền — hay xảy ra khi copy template nhiều lần
    seen: dict[str, list[str]] = {}
    for flow in flows:
        fid = flow.get("id", Path(flow["_path"]).stem)
        seen.setdefault(fid, []).append(flow["_path"])
    for fid, paths in seen.items():
        if len(paths) > 1:
            print(f"  ✗ id trùng '{fid}' ở {len(paths)} file: {', '.join(paths)}")
            errors += 1
        if "TODO" in str(fid):
            print(f"  ✗ {paths[0]}: id còn là placeholder ('{fid}') — chưa điền template")
            errors += 1

    for flow in flows:
        fid = flow.get("id", Path(flow["_path"]).stem)
        # Nếu id chưa điền hoặc trùng, hiển thị đường dẫn để phân biệt được file
        label = fid if (len(seen.get(fid, [])) == 1 and "TODO" not in str(fid)) \
                else flow["_path"]

        for required in ("id", "name", "severity", "steps"):
            if required not in flow:
                print(f"  ✗ {label}: thiếu field bắt buộc '{required}'")
                errors += 1

        sev = flow.get("severity", {}) or {}
        for flag in ("idempotent", "reversible", "compensating_action"):
            if flag not in sev:
                print(f"  ⚠ {label}: thiếu severity.{flag} "
                      f"(cờ khuếch đại, ảnh hưởng xếp hạng)")
                warnings += 1

        # Drift: entity ref không resolve được = lỗi thật, không phải cảnh báo
        for step in flow.get("steps", []):
            sid = step.get("id", "?")
            for ent in step.get("entities", []):
                if not any(entity_matches(ent, f) for f in tracked):
                    print(f"  ✗ {label}/{sid}: entity không resolve: {ent}")
                    errors += 1
                else:
                    warnings += check_glob_breadth(ent, tracked, label, sid)
            for iv in step.get("invariants", []) or []:
                e, w = check_invariant(iv, label, sid)
                errors += e
                warnings += w

        op = flow.get("operational", {}) or {}
        if op.get("executions_per_day") is None:
            print(f"  ⚠ {label}: thiếu operational.executions_per_day "
                  f"(không tính được canary blindness)")
            warnings += 1

        if sev.get("class") == "critical":
            obs = op.get("observability", {}) or {}
            if not obs.get("alertable_metric"):
                print(f"  ⚠ {label}: flow critical không có alertable_metric")
                warnings += 1

        n_conf = sum(
            1 for s in flow.get("steps", [])
            for iv in s.get("invariants", [])
            if iv.get("status") == "confirmed"
        )
        if n_conf == 0:
            print(f"  ⚠ {label}: chưa có invariant nào status=confirmed")
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


# ──────────────────── CloudWatch Logs Insights ────────────────────

INSIGHTS_QUERY = r"""
fields @message
| parse @message /(?<m>[A-Z]{3,7}) (?<p>\/[^\s?"]*)/
| filter ispresent(p)
| stats count(*) as cnt by m, p
| sort cnt desc
| limit 300
"""


def aws(*args) -> str:
    """Chạy aws CLI. Thoát với hướng dẫn SSO nếu chưa đăng nhập."""
    try:
        r = subprocess.run(["aws", *args], capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit(
            "Chưa cài AWS CLI.\n\n"
            "  macOS:  brew install awscli\n"
            "  Linux:  https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html\n\n"
            "Cài xong thì: aws sso login --profile <profile>"
        )
    if r.returncode != 0:
        err = r.stderr.strip()
        if any(k in err for k in ("ExpiredToken", "SSO", "credentials",
                                  "Unable to locate", "InvalidClientTokenId",
                                  "AccessDenied", "sso session")):
            sys.exit(
                "Chưa đăng nhập AWS (hoặc token đã hết hạn).\n\n"
                "  aws sso login --profile <profile>\n"
                "  export AWS_PROFILE=<profile>\n\n"
                "Rồi chạy lại lệnh này.\n"
                f"\nChi tiết: {err[:300]}"
            )
        sys.exit(f"aws {' '.join(args)} thất bại:\n{err[:500]}")
    return r.stdout


def cloudwatch_check() -> None:
    ident = json.loads(aws("sts", "get-caller-identity", "--output", "json"))
    print(f"✓ Đã đăng nhập: {ident.get('Arn', '?')}")
    print(f"  Account: {ident.get('Account', '?')}\n")

    groups = json.loads(aws("logs", "describe-log-groups",
                            "--output", "json"))["logGroups"]
    if not groups:
        print("Không thấy log group nào. Kiểm tra --region.")
        return
    print(f"{len(groups)} log group:")
    for g in sorted(groups, key=lambda x: -x.get("storedBytes", 0))[:25]:
        mb = g.get("storedBytes", 0) / 1e6
        print(f"  {mb:>10,.0f} MB  {g['logGroupName']}")
    print("\n→ Chọn 1 group rồi chạy:")
    print("  fra.py cloudwatch --group <tên> --days 30")


def cloudwatch_traffic(group: str, days: int) -> dict:
    import time
    end = int(time.time())
    start = end - days * 86400

    qid = json.loads(aws(
        "logs", "start-query",
        "--log-group-name", group,
        "--start-time", str(start),
        "--end-time", str(end),
        "--query-string", INSIGHTS_QUERY,
        "--output", "json",
    ))["queryId"]

    print(f"Query {qid} đang chạy", end="", flush=True)
    for _ in range(120):                      # tối đa ~4 phút
        time.sleep(2)
        res = json.loads(aws("logs", "get-query-results",
                             "--query-id", qid, "--output", "json"))
        if res["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        print(".", end="", flush=True)
    else:
        sys.exit("\nQuery quá lâu. Thử --days nhỏ hơn.")
    print()

    if res["status"] != "Complete":
        sys.exit(f"Query {res['status']}. Thử --days nhỏ hơn hoặc kiểm log format.")

    counts = Counter()
    for row in res["results"]:
        f = {c["field"]: c["value"] for c in row}
        if "m" in f and "p" in f:
            counts[f"{f['m']} {normalize_path(f['p'])}"] += int(f.get("cnt", 0))

    scanned = res.get("statistics", {}).get("recordsScanned", 0)
    matched = res.get("statistics", {}).get("recordsMatched", 0)
    if scanned and matched / scanned < 0.5:
        print(f"⚠ Chỉ khớp {matched:,.0f}/{scanned:,.0f} record "
              f"({matched/scanned:.0%}). Log format có thể không phải HTTP "
              f"access log — kiểm lại group.", file=sys.stderr)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": f"cloudwatch:{group}",
        "window_days": days,
        "records_scanned": scanned,
        "records_matched": matched,
        "endpoints": {ep: round(n / days, 2) for ep, n in counts.most_common(300)},
    }


# ─────────────────── hotfix mapping & validation ──────────────────

HOTFIX_GREP = r"hotfix\|revert\|urgent\|rollback\|^fix\|INC-\|critical"

# Loại các "fix" không phải sửa lỗi production
NOISE = re.compile(
    r"\b(phpstan|eslint|lint|typo|phpdoc|comment|format|prettier|"
    r"ci|cd|pipeline|test|spec|docs?|readme|changelog|"
    r"review comment|address review)\b", re.I,
)


def find_hotfixes(until: str = "", since: str = "", limit: int = 40) -> list:
    args = ["log", "--grep", HOTFIX_GREP, "-i", "--no-merges",
            "--pretty=format:%H|%ad|%s", "--date=short"]
    if until:
        args.append(f"--until={until}")
    if since:
        args.append(f"--since={since}")
    out = git(*args)

    rows = []
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        sha, date, subj = parts
        if NOISE.search(subj):
            continue
        rows.append((sha, date, subj))
        if len(rows) >= limit:
            break
    return rows


def hotfix_map(cfg, until: str = "", since: str = "", limit: int = 40,
               label: str = "") -> dict:
    """Với mỗi hotfix: chạy analyze trên chính commit đó, xem flow nào bắt được.

    Đây là kiểm chứng có ý nghĩa nhất: hotfix là bằng chứng một sự cố ĐÃ xảy ra.
    """
    rows = find_hotfixes(until, since, limit)
    if not rows:
        print(f"Không tìm thấy hotfix nào trong cửa sổ {label or 'này'}.")
        return {"total": 0, "caught": 0, "misses": [], "by_flow": Counter()}

    print(f"\n{'commit':<10}{'ngày':<12}{'flow bắt được':<44}subject")
    print("─" * 108)

    caught, misses = 0, []
    by_flow = Counter()

    for sha, date, subj in rows:
        try:
            rep = analyze(f"{sha}^", sha, cfg, use_cochange=False)
        except SystemExit:
            continue
        ids = [f["flow_id"] for f in rep["flows_affected"]]
        if ids:
            caught += 1
            for i in ids:
                by_flow[i] += 1
        else:
            misses.append((sha, date, subj))
        tag = ", ".join(ids) if ids else "— KHÔNG BẮT ĐƯỢC"
        print(f"{sha[:8]:<10}{date:<12}{tag[:42]:<44}{subj[:44]}")

    total = len(rows)
    print(f"\nRecall: {caught}/{total} ({caught/total:.0%}) hotfix được gắn cờ")
    if by_flow:
        print("\nFlow nào bắt được nhiều nhất:")
        for fid, n in by_flow.most_common():
            print(f"  {n:>3}  {fid}")
    if misses:
        print(f"\n{len(misses)} hotfix KHÔNG bắt được — vùng tối của manifest:")
        for sha, date, subj in misses[:12]:
            print(f"  {sha[:8]}  {date}  {subj[:70]}")
        print("\n  → Xem các commit này chạm file nào, rồi bổ sung vào entities:")
        print(f"     git show --stat --format='' {misses[0][0][:8]}")

    return {"total": total, "caught": caught, "misses": misses,
            "by_flow": by_flow}


def validate(cfg, split_date: str) -> int:
    """Kiểm chứng hai cửa sổ: gần đây (có thể đã ảnh hưởng tới manifest) và
    holdout (cũ hơn split_date, chưa ai xem khi xây manifest).

    Chỉ recall trên holdout mới là bằng chứng thật. Recall trên cửa sổ gần đây
    bị nhiễu vì manifest thường được điều chỉnh theo đúng các hotfix đó.
    """
    print("═" * 108)
    print(f"CỬA SỔ GẦN ĐÂY (từ {split_date}) — có thể đã ảnh hưởng tới manifest")
    print("═" * 108)
    recent = hotfix_map(cfg, since=split_date, label="gần đây")

    print()
    print("═" * 108)
    print(f"HOLDOUT (trước {split_date}) — KIỂM CHỨNG ĐỘC LẬP")
    print("═" * 108)
    hold = hotfix_map(cfg, until=split_date, label="holdout")

    print()
    print("═" * 108)
    print("KẾT LUẬN")
    print("═" * 108)
    for name, r in (("gần đây", recent), ("holdout", hold)):
        if r["total"]:
            print(f"  {name:<10} recall {r['caught']}/{r['total']} "
                  f"({r['caught']/r['total']:.0%})")
        else:
            print(f"  {name:<10} không có dữ liệu")

    if not hold["total"]:
        print("\n  ⚠ Không có holdout. Recall ở trên KHÔNG phải kiểm chứng độc lập:")
        print("    manifest thường được điều chỉnh theo chính các hotfix đã xem.")
        print("    Thử --split-date sớm hơn để tách được tập holdout.")
        return 0

    hr = hold["caught"] / hold["total"]
    print()
    if hr >= 0.6:
        print(f"  ✓ Holdout recall {hr:.0%} ≥ 60% — ĐẠT. Được phép bật CI comment-only.")
        return 0
    print(f"  ✗ Holdout recall {hr:.0%} < 60% — CHƯA ĐẠT.")
    print("    Manifest chưa phủ đủ. Xem danh sách 'KHÔNG bắt được' ở trên,")
    print("    bổ sung entities hoặc thêm flow, rồi chạy lại.")
    print("    Đừng bật CI khi chưa đạt — team sẽ tin một hệ thống chưa kiểm chứng.")
    return 1


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
    # Lấy nhiều hơn n vì sẽ lọc bỏ back-merge
    raw = git("log", "--merges", "-n", str(n * 2),
              "--pretty=format:%H|%s").splitlines()
    if not raw:
        raw = git("log", "-n", str(n), "--pretty=format:%H|%s").splitlines()

    # Back-merge (kéo base vào feature branch): sha^ là đầu feature branch,
    # nên diff sha^..sha = mọi thứ base có mà branch chưa có -> chạm mọi flow.
    # Không phản ánh thay đổi của PR nào. Phải loại.
    BACKMERGE = re.compile(
        r"^Merge (branch|remote-tracking branch) .+ into (?!master\b|main\b)",
        re.I,
    )

    entries, skipped = [], 0
    for e in raw:
        if "|" not in e:
            continue
        sha, subject = e.split("|", 1)
        if BACKMERGE.match(subject):
            skipped += 1
            continue
        entries.append((sha, subject))
        if len(entries) >= n:
            break

    flagged = 0
    flow_hits = Counter()

    for sha, subject in entries:
        try:
            rep = analyze(f"{sha}^", sha, cfg, use_cochange=False)
        except SystemExit:
            continue
        flows = rep["flows_affected"]
        if flows:
            flagged += 1
        for f in flows:
            flow_hits[f["flow_id"]] += 1
        tag = ", ".join(f"{f['flow_id']}({f['severity_class']})"
                        for f in flows) or "—"
        print(f"{sha[:8]}  {len(flows)} flow  {tag[:60]:<60}  {subject[:44]}")

    total = len(entries)
    if not total:
        return

    print()
    if skipped:
        print(f"Đã bỏ {skipped} back-merge (diff không phản ánh thay đổi của PR).")
    rate = flagged / total
    print(f"Gắn cờ {flagged}/{total} commit ({rate:.0%}).")

    # Dưới 10 commit thì tỷ lệ chưa nói lên gì — không cảnh báo để tránh báo động giả
    enough = total >= 10
    if not enough:
        print(f"  (mẫu {total} commit là quá nhỏ để đánh giá tỷ lệ — "
              f"chạy --last 30 trở lên)")
    elif rate > 0.5:
        print("  ⚠ >50% — closure có thể quá rộng. Tăng cochange.min_confidence,")
        print("    hoặc thu hẹp entities dùng glob quá lớn.")

    print("\nTần suất mỗi flow bị gắn cờ:")
    for fid, cnt in flow_hits.most_common():
        share = cnt / total
        warn = "  ⚠ entities có thể quá rộng" if (enough and share > 0.4) else ""
        print(f"  {cnt:>3}/{total} ({share:>4.0%})  {fid}{warn}")


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

    hm = sub.add_parser("hotfix-map")
    hm.add_argument("--until", default="", help="chỉ hotfix trước ngày này")
    hm.add_argument("--since", default="", help="chỉ hotfix sau ngày này")
    hm.add_argument("--limit", type=int, default=40)

    va = sub.add_parser("validate")
    va.add_argument("--split-date", required=True,
                    help="ranh giới holdout, ví dụ 2026-06-01")

    sub.add_parser("aws-check")

    cw = sub.add_parser("cloudwatch")
    cw.add_argument("--group", required=True, help="tên CloudWatch log group")
    cw.add_argument("--days", type=int, default=30)
    cw.add_argument("--top", type=int, default=40)

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

    elif args.cmd == "aws-check":
        cloudwatch_check()

    elif args.cmd == "cloudwatch":
        data = cloudwatch_traffic(args.group, args.days)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        out = CACHE_DIR / "traffic.json"
        out.write_text(json.dumps(data, indent=2))
        print(f"✓ {out}  ({data['records_matched']:,.0f} record khớp)\n")
        print(f"{'lần/ngày':>12}  endpoint")
        for ep, rate in list(data["endpoints"].items())[:args.top]:
            print(f"{rate:>12,.1f}  {ep}")
        print("\n→ Dùng các số này cho operational.executions_per_day trong manifest.")

    elif args.cmd == "hotfix-map":
        hotfix_map(cfg, until=args.until, since=args.since, limit=args.limit)

    elif args.cmd == "validate":
        sys.exit(validate(cfg, args.split_date))

    elif args.cmd == "backtest":
        backtest(args.last, cfg)


if __name__ == "__main__":
    main()
