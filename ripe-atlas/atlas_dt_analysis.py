#!/usr/bin/env python3
"""RIPE Atlas analysis: localize the Deutsche Telekom <-> GTT <-> CAP bottleneck.

Discriminates four hypotheses for the degraded gaming/Cloudflare experience of
Telekom (AS3320) customers:

  H1  DTAG <-> GTT interconnect under-dimensioned
      -> RTT inflation / loss starts at the AS3320 -> AS3257 boundary hop,
         peak hours only, control ISPs (Vodafone/O2) unaffected.
  H2  GTT <-> CAP (Cloudflare/EA/Valve/...) interconnect under-dimensioned
      -> path is clean through GTT, degradation starts at the last AS3257 hop
         towards the CAP edge.
  H3  GTT backbone congestion / suboptimal internal path
      -> degradation starts BETWEEN two AS3257 hops (e.g. FRA -> AMS inside GTT).
  H4  CAP anycast steering sends DTAG users to a remote PoP
      -> DNS CHAOS colo check shows DTAG probes landing in a non-DE/non-nearby
         Cloudflare colo while control-ISP probes land locally.

Subcommands:
  create    schedule traceroute + ping + DNS-colo measurements (needs API key)
  fetch     download results + probe metadata for measurement IDs (no key)
  analyze   localize per-AS-boundary degradation, peak vs off-peak, write report

Typical run:
  export ATLAS_API_KEY=...   # key with 'Measurement creation' permission
  python3 atlas_dt_analysis.py create --duration-hours 48 > msm_ids.json
  # ... wait until the campaign has run (>= a few hours incl. one 19-23h CET peak)
  python3 atlas_dt_analysis.py fetch  --ids-file msm_ids.json --out data/
  python3 atlas_dt_analysis.py analyze --data data/ --report report.md

Only dependency: requests  (pip install requests)
"""

import argparse
import collections
import datetime as dt
import json
import os
import statistics
import sys
import time
from pathlib import Path

import requests

API = "https://atlas.ripe.net/api/v2"
RIPESTAT_NETINFO = "https://stat.ripe.net/data/network-info/data.json"

DTAG_ASN = 3320
GTT_ASN = 3257

# Control eyeball networks in Germany. If the problem is specific to Telekom's
# interconnection strategy, these should be clean against the same targets.
PROBE_GROUPS = {
    3320: ("Deutsche Telekom", 30),
    3209: ("Vodafone DE", 12),
    6805: ("Telefonica/O2 DE", 12),
    31334: ("Vodafone Kabel (KDG)", 8),
}

# Default targets. resolve_on_probe=True means every probe resolves the
# hostname itself and traces to "its" CDN edge -- exactly what a customer sees.
# Add concrete game-server IPs (taken from a live session on a Telekom line,
# e.g. via `ss -tunp` while playing) with --target for the strongest evidence.
DEFAULT_TARGETS = [
    "1.1.1.1",              # Cloudflare anycast (also used for the colo check)
    "www.cloudflare.com",   # Cloudflare HTTP edge
    "gateway.discord.gg",   # Discord (Cloudflare-fronted, latency-sensitive)
]

PEAK_HOURS = range(19, 24)      # 19:00-23:59 Europe/Berlin
OFFPEAK_HOURS = range(2, 7)     # 02:00-06:59 Europe/Berlin

# Cloudflare colos considered "fine" for German eyeballs (IATA codes as
# returned by `id.server CH TXT`, e.g. "FRA", "fra01").
NEARBY_COLOS = {"FRA", "MUC", "DUS", "HAM", "BER", "STR", "TXL", "AMS", "PRG", "VIE", "ZRH", "CDG", "BRU", "LUX", "CPH", "WAW"}


def _berlin_hour(ts: int) -> int:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.fromtimestamp(ts, ZoneInfo("Europe/Berlin")).hour
    except Exception:
        # tzdata missing: fall back to CET+1h DST-naive approximation (UTC+2)
        return dt.datetime.utcfromtimestamp(ts + 7200).hour


def _auth(args) -> dict:
    key = args.api_key or os.environ.get("ATLAS_API_KEY", "")
    if not key:
        sys.exit("ATLAS_API_KEY not set (or pass --api-key). "
                 "Create one with 'Measurement creation' permission at "
                 "https://atlas.ripe.net/keys/")
    return {"Authorization": f"Key {key}"}


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------

def cmd_create(args):
    targets = args.target or DEFAULT_TARGETS
    stop = int(time.time()) + args.duration_hours * 3600
    probes = [
        {"type": "asn", "value": asn, "requested": n}
        for asn, (_, n) in PROBE_GROUPS.items()
    ]

    definitions = []
    for t in targets:
        definitions.append({
            "type": "traceroute", "af": 4, "target": t,
            "description": f"DT-GTT analysis traceroute {t}",
            "protocol": "ICMP", "paris": 16, "resolve_on_probe": True,
            "interval": args.traceroute_interval,
        })
        definitions.append({
            "type": "ping", "af": 4, "target": t,
            "description": f"DT-GTT analysis ping {t}",
            "packets": 3, "resolve_on_probe": True,
            "interval": args.ping_interval,
        })
    # Which Cloudflare colo serves each probe (H4). CHAOS TXT id.server
    # against 1.1.1.1 returns the colo code of the edge that answered.
    definitions.append({
        "type": "dns", "af": 4, "target": "1.1.1.1",
        "description": "DT-GTT analysis cloudflare colo check",
        "query_class": "CHAOS", "query_type": "TXT",
        "query_argument": "id.server", "use_probe_resolver": False,
        "udp_payload_size": 512, "interval": args.traceroute_interval,
    })

    body = {"definitions": definitions, "probes": probes,
            "is_oneoff": args.oneoff, "start_time": int(time.time()) + 120}
    if not args.oneoff:
        body["stop_time"] = stop

    r = requests.post(f"{API}/measurements/", json=body, headers=_auth(args), timeout=60)
    if r.status_code != 201:
        sys.exit(f"measurement creation failed: HTTP {r.status_code}\n{r.text}")
    ids = r.json()["measurements"]
    manifest = {
        "created": dt.datetime.now(dt.timezone.utc).isoformat(),
        "measurement_ids": ids,
        "targets": targets,
        "probe_groups": {str(k): v[0] for k, v in PROBE_GROUPS.items()},
        "oneoff": args.oneoff,
    }
    print(json.dumps(manifest, indent=2))
    print(f"\ncreated {len(ids)} measurements; track them at "
          f"https://atlas.ripe.net/measurements/{ids[0]}/ etc.", file=sys.stderr)


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------

def cmd_fetch(args):
    manifest = json.loads(Path(args.ids_file).read_text())
    ids = manifest["measurement_ids"] if isinstance(manifest, dict) else manifest
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    probe_ids = set()
    for mid in ids:
        meta = requests.get(f"{API}/measurements/{mid}/", timeout=60).json()
        results = requests.get(f"{API}/measurements/{mid}/results/?format=json",
                               timeout=300).json()
        (out / f"msm_{mid}.json").write_text(json.dumps(
            {"meta": {"id": mid, "type": meta.get("type"),
                      "target": meta.get("target"),
                      "description": meta.get("description")},
             "results": results}))
        probe_ids.update(r.get("prb_id") for r in results if r.get("prb_id"))
        print(f"msm {mid}: {meta.get('type'):>10} {meta.get('target', ''):<25} "
              f"{len(results)} results", file=sys.stderr)

    # probe -> ASN/country mapping, needed to group results by ISP
    probes = {}
    pl = sorted(p for p in probe_ids if p)
    for i in range(0, len(pl), 100):
        chunk = ",".join(map(str, pl[i:i + 100]))
        r = requests.get(f"{API}/probes/?id__in={chunk}&page_size=100", timeout=60)
        for p in r.json().get("results", []):
            probes[p["id"]] = {"asn": p.get("asn_v4"),
                               "country": p.get("country_code")}
    (out / "probes.json").write_text(json.dumps(probes, indent=1))
    print(f"fetched metadata for {len(probes)} probes -> {out}/", file=sys.stderr)


# --------------------------------------------------------------------------
# analyze
# --------------------------------------------------------------------------

class AsnResolver:
    """IP -> origin ASN via RIPEstat, with a persistent on-disk cache."""

    def __init__(self, cache_path: Path):
        self.path = cache_path
        self.cache = {}
        if cache_path.exists():
            self.cache = json.loads(cache_path.read_text())

    def lookup(self, ip: str):
        if not ip or ip.startswith(("10.", "192.168.", "100.6", "172.")):
            return None
        if ip in self.cache:
            return self.cache[ip]
        asn = None
        try:
            r = requests.get(RIPESTAT_NETINFO, params={"resource": ip}, timeout=15)
            asns = r.json().get("data", {}).get("asns") or []
            asn = int(asns[0]) if asns else None
        except Exception:
            return None  # do not cache transient failures
        self.cache[ip] = asn
        return asn

    def save(self):
        self.path.write_text(json.dumps(self.cache))


def _segment_label(asn):
    if asn == DTAG_ASN:
        return "DTAG"
    if asn == GTT_ASN:
        return "GTT"
    if asn is None:
        return "?"
    return f"AS{asn}"


def _hop_min_rtt(hop):
    rtts = [p["rtt"] for p in hop.get("result", []) if isinstance(p, dict) and "rtt" in p]
    return min(rtts) if rtts else None


def analyze_traceroutes(msm, probes, resolver, findings):
    """Attribute the biggest RTT step-up of each traceroute to an AS boundary."""
    target = msm["meta"]["target"]
    for res in msm["results"]:
        prb = probes.get(str(res.get("prb_id"))) or probes.get(res.get("prb_id"))
        if not prb or prb.get("asn") not in PROBE_GROUPS:
            continue
        hops = []
        for hop in res.get("result", []):
            ip = next((p.get("from") for p in hop.get("result", [])
                       if isinstance(p, dict) and p.get("from")), None)
            rtt = _hop_min_rtt(hop)
            if ip is None and rtt is None:
                continue
            hops.append((ip, resolver.lookup(ip) if ip else None, rtt))
        if len(hops) < 3:
            continue

        # largest RTT step between consecutive responding hops + its AS boundary
        best = None
        prev_rtt, prev_seg = None, None
        for ip, asn, rtt in hops:
            seg = _segment_label(asn)
            if rtt is not None and prev_rtt is not None:
                step = rtt - prev_rtt
                boundary = f"{prev_seg}->{seg}" if seg != prev_seg else f"within {seg}"
                if best is None or step > best[0]:
                    best = (step, boundary)
            if rtt is not None:
                prev_rtt, prev_seg = rtt, seg
        if best is None:
            continue

        dest_rtt = hops[-1][2]
        reached = res.get("destination_ip_responded", True) and dest_rtt is not None
        findings.append({
            "kind": "traceroute", "target": target,
            "isp": PROBE_GROUPS[prb["asn"]][0],
            "hour": _berlin_hour(res["timestamp"]),
            "max_step_ms": round(best[0], 1), "boundary": best[1],
            "dest_rtt": dest_rtt, "reached": reached,
            "via_gtt": any(a == GTT_ASN for _, a, _ in hops),
        })


def analyze_pings(msm, probes, findings):
    target = msm["meta"]["target"]
    for res in msm["results"]:
        prb = probes.get(str(res.get("prb_id"))) or probes.get(res.get("prb_id"))
        if not prb or prb.get("asn") not in PROBE_GROUPS:
            continue
        sent, rcvd = res.get("sent", 0), res.get("rcvd", 0)
        if not sent:
            continue
        findings.append({
            "kind": "ping", "target": target,
            "isp": PROBE_GROUPS[prb["asn"]][0],
            "hour": _berlin_hour(res["timestamp"]),
            "loss_pct": round(100 * (sent - rcvd) / sent, 1),
            "rtt": res.get("avg") if res.get("avg", -1) > 0 else None,
        })


def analyze_colos(msm, probes, findings):
    for res in msm["results"]:
        prb = probes.get(str(res.get("prb_id"))) or probes.get(res.get("prb_id"))
        if not prb or prb.get("asn") not in PROBE_GROUPS:
            continue
        answers = []
        for rs in ([res.get("result")] if isinstance(res.get("result"), dict)
                   else res.get("resultset", []) or []):
            if not rs:
                continue
            abuf = rs.get("answers") or rs.get("result", {}).get("answers") or []
            for a in abuf:
                data = a.get("TXT") or a.get("RDATA") or []
                answers.extend(data if isinstance(data, list) else [data])
        colo = next((str(a).strip('"').upper()[:3] for a in answers if a), None)
        if not colo:
            continue
        findings.append({
            "kind": "colo", "isp": PROBE_GROUPS[prb["asn"]][0],
            "hour": _berlin_hour(res["timestamp"]), "colo": colo,
            "nearby": colo in NEARBY_COLOS,
        })


def _med(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 1) if vals else None


def cmd_analyze(args):
    data = Path(args.data)
    probes = json.loads((data / "probes.json").read_text())
    resolver = AsnResolver(data / "asn_cache.json")
    findings = []
    for f in sorted(data.glob("msm_*.json")):
        msm = json.loads(f.read_text())
        t = msm["meta"]["type"]
        if t == "traceroute":
            analyze_traceroutes(msm, probes, resolver, findings)
        elif t == "ping":
            analyze_pings(msm, probes, findings)
        elif t == "dns":
            analyze_colos(msm, probes, findings)
    resolver.save()
    if not findings:
        sys.exit("no findings -- did fetch produce results yet?")

    lines = ["# RIPE Atlas: DT -> GTT -> CAP bottleneck localization", ""]
    lines.append(f"Generated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC. "
                 f"Peak = 19-23h, off-peak = 02-06h Europe/Berlin.")

    # ---- ping: loss & RTT, peak vs off-peak, per ISP/target -------------
    pings = [f for f in findings if f["kind"] == "ping"]
    if pings:
        lines += ["", "## End-to-end loss / RTT (ping)", "",
                  "| target | ISP | RTT peak | RTT off-peak | loss% peak | loss% off-peak | n |",
                  "|---|---|---|---|---|---|---|"]
        key = lambda f: (f["target"], f["isp"])
        for (tgt, isp), grp in sorted(_groupby(pings, key).items()):
            pk = [g for g in grp if g["hour"] in PEAK_HOURS]
            op = [g for g in grp if g["hour"] in OFFPEAK_HOURS]
            lines.append(f"| {tgt} | {isp} | {_med([g['rtt'] for g in pk])} | "
                         f"{_med([g['rtt'] for g in op])} | "
                         f"{_med([g['loss_pct'] for g in pk])} | "
                         f"{_med([g['loss_pct'] for g in op])} | {len(grp)} |")

    # ---- traceroute: where does the largest RTT step occur? -------------
    traces = [f for f in findings if f["kind"] == "traceroute"]
    h_evidence = collections.Counter()
    if traces:
        lines += ["", "## Largest per-trace RTT step-up, attributed to AS boundary",
                  "", "(peak-hour traceroutes from Telekom probes only; the boundary",
                  "where congestion-grade step-ups cluster localizes the bottleneck)", "",
                  "| target | boundary | peak traces | median step ms (peak) | median step ms (off-peak) |",
                  "|---|---|---|---|---|"]
        dt_traces = [f for f in traces if f["isp"] == "Deutsche Telekom"]
        n_peak_total = sum(1 for f in dt_traces if f["hour"] in PEAK_HOURS)
        key = lambda f: (f["target"], f["boundary"])
        for (tgt, bnd), grp in sorted(_groupby(dt_traces, key).items(),
                                      key=lambda kv: -len(kv[1])):
            pk = [g["max_step_ms"] for g in grp if g["hour"] in PEAK_HOURS]
            op = [g["max_step_ms"] for g in grp if g["hour"] in OFFPEAK_HOURS]
            if not pk:
                continue
            lines.append(f"| {tgt} | {bnd} | {len(pk)} | {_med(pk)} | {_med(op)} |")
            step = _med(pk) or 0
            if step >= args.step_threshold:
                # score = % of all DT peak traces whose worst step sits here
                share = round(100 * len(pk) / max(n_peak_total, 1))
                if bnd == "DTAG->GTT":
                    h_evidence["H1"] += share
                elif bnd.startswith("GTT->"):
                    h_evidence["H2"] += share
                elif bnd == "within GTT":
                    h_evidence["H3"] += share
        gtt_share = sum(f["via_gtt"] for f in dt_traces) / max(len(dt_traces), 1)
        lines.append(f"\nShare of Telekom traceroutes transiting GTT (AS3257): "
                     f"{gtt_share:.0%}")

    # ---- colo steering (H4) ---------------------------------------------
    colos = [f for f in findings if f["kind"] == "colo"]
    if colos:
        lines += ["", "## Cloudflare colo per ISP (DNS CHAOS id.server @1.1.1.1)", "",
                  "| ISP | colos seen (count) | % non-nearby |", "|---|---|---|"]
        for isp, grp in sorted(_groupby(colos, lambda f: f["isp"]).items()):
            cnt = collections.Counter(g["colo"] for g in grp)
            far = 100 * sum(1 for g in grp if not g["nearby"]) / len(grp)
            tops = ", ".join(f"{c} ({n})" for c, n in cnt.most_common(5))
            lines.append(f"| {isp} | {tops} | {far:.0f}% |")
            if isp == "Deutsche Telekom" and far > 25:
                h_evidence["H4"] += int(far)

    # ---- verdict ---------------------------------------------------------
    lines += ["", "## Hypothesis scoreboard (heuristic)", ""]
    names = {"H1": "DTAG<->GTT interconnect congested",
             "H2": "GTT<->CAP interconnect congested",
             "H3": "GTT backbone congested",
             "H4": "CAP anycast steering to remote PoP"}
    if h_evidence:
        lines.append("Scores are 0-100: for H1-H3 the share of Telekom "
                     "peak-hour traceroutes whose largest RTT step sits on "
                     "that boundary; for H4 the share of Telekom colo checks "
                     "landing in a non-nearby PoP.\n")
        for h, score in h_evidence.most_common():
            lines.append(f"- **{h} — {names[h]}**: score {min(score, 100)}")
        lines.append("\nInterpret together with the ping table: a genuine "
                     "capacity problem shows peak-hour loss/RTT inflation for "
                     "Telekom probes that control ISPs do not show.")
    else:
        lines.append("No boundary showed a peak-hour RTT step above "
                     f"{args.step_threshold} ms — either the campaign missed "
                     "peak hours or the problem is not on the forward path "
                     "(consider reverse-path / IPv6 / per-game-server runs).")

    report = "\n".join(lines) + "\n"
    Path(args.report).write_text(report)
    print(report)


def _groupby(items, key):
    out = collections.defaultdict(list)
    for it in items:
        out[key(it)].append(it)
    return out


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="schedule the measurement campaign")
    c.add_argument("--api-key")
    c.add_argument("--target", action="append",
                   help="extra/override targets (repeatable); default: %s" % DEFAULT_TARGETS)
    c.add_argument("--duration-hours", type=int, default=48)
    c.add_argument("--traceroute-interval", type=int, default=1800)
    c.add_argument("--ping-interval", type=int, default=600)
    c.add_argument("--oneoff", action="store_true",
                   help="single cheap snapshot instead of a 48h campaign")
    c.set_defaults(func=cmd_create)

    f = sub.add_parser("fetch", help="download results (no API key needed)")
    f.add_argument("--ids-file", required=True,
                   help="JSON manifest from 'create' (or a bare list of IDs)")
    f.add_argument("--out", default="data")
    f.set_defaults(func=cmd_fetch)

    a = sub.add_parser("analyze", help="localize bottleneck, write markdown report")
    a.add_argument("--data", default="data")
    a.add_argument("--report", default="report.md")
    a.add_argument("--step-threshold", type=float, default=15.0,
                   help="min median peak-hour RTT step (ms) to count as evidence")
    a.set_defaults(func=cmd_analyze)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
