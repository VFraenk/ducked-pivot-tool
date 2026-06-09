# RIPE Atlas analysis: Telekom ↔ GTT ↔ CAP bottleneck localization

Toolkit to determine **where** the degraded experience of Deutsche Telekom
(AS3320) customers towards Cloudflare / gaming providers actually originates,
discriminating four hypotheses:

| # | Hypothesis | Measurement signature |
|---|---|---|
| H1 | DTAG ↔ GTT interconnect under-dimensioned | RTT step-up / loss begins exactly at the AS3320 → AS3257 boundary hop, **peak hours only** (19–23h CET); Vodafone/O2 probes to the same target are clean |
| H2 | GTT ↔ CAP interconnect under-dimensioned | Path is clean through GTT; degradation begins at the **last** AS3257 hop towards the Cloudflare/EA/Valve edge |
| H3 | GTT backbone congested / suboptimal | Degradation begins **between two AS3257 hops** (e.g. FRA→AMS inside GTT) |
| H4 | CAP anycast steers DTAG traffic to a remote PoP | DNS `CHAOS TXT id.server @1.1.1.1` returns a distant colo (not FRA/MUC/DUS/HAM/AMS/…) for Telekom probes while control ISPs land locally |

## Design

- **Probe groups:** AS3320 (Telekom, 30 probes) vs. controls AS3209 (Vodafone),
  AS6805 (Telefónica/O2), AS31334 (Vodafone Kabel). The controls separate
  "Telekom's interconnection strategy" from "the CAP/GTT side is broken for
  everyone".
- **Measurements per target:** Paris-ICMP traceroute (path + per-hop RTT,
  every 30 min), ping (end-to-end loss/jitter, every 10 min), plus one DNS
  CHAOS colo-check measurement against 1.1.1.1 (H4).
- **Peak vs. off-peak:** the campaign runs ≥48 h; analysis buckets results
  into 19–23h vs. 02–06h Europe/Berlin. A capacity problem is *defined* by
  the delta between those buckets — a constant RTT offset is routing, not
  congestion.
- **Localization:** for every traceroute the largest RTT step between
  consecutive responding hops is attributed to the AS boundary where it
  occurs (DTAG→GTT, within GTT, GTT→CAP). Congestion-grade step-ups
  clustering on one boundary at peak hours localize the bottleneck.
  Hop IPs are mapped to ASNs via RIPEstat with an on-disk cache.

## Running it

```bash
pip install requests
export ATLAS_API_KEY=...        # needs 'Measurement creation' permission

# 1. cheap smoke test (one-off, a few hundred credits)
python3 atlas_dt_analysis.py create --oneoff > msm_ids.json

# 2. real campaign (48 h)
python3 atlas_dt_analysis.py create --duration-hours 48 > msm_ids.json

# 3. after >= one evening peak has been captured
python3 atlas_dt_analysis.py fetch --ids-file msm_ids.json --out data/
python3 atlas_dt_analysis.py analyze --data data/ --report report.md
```

`fetch` and `analyze` need **no API key** (results of public measurements are
open), so anyone can re-verify the analysis from the measurement IDs alone —
useful if the report is to be cited in a regulatory/peering-policy argument.

### Cost estimate (RIPE Atlas credits)

Defaults (3 targets, ~62 probes, 48 h): roughly 60 traceroute-results/probe/target
(~30–60 credits each) plus pings (~3 credits each) ≈ **400–700k credits**.
Reduce with `--traceroute-interval 3600`, fewer probes in `PROBE_GROUPS`, or
shorter `--duration-hours`. A `--oneoff` smoke test costs a few hundred credits.
Credits: host a probe/anchor, or ask in the RIPE Atlas community — credits for
documented research into interconnection disputes are routinely sponsored.

### Choosing game-server targets

Anycast/CDN targets are built in. For EA/Valve/Riot the strongest evidence
comes from the *actual* server IPs a Telekom customer plays on: capture them
during a session (`ss -tunp` / Wireshark) and add them with
`--target <ip>` (repeatable). Game traffic is often regionalized, so
the path Telekom probes take to e.g. Valve Frankfurt (AS32590) may differ
completely from the Cloudflare path.

## Interpreting the report

- **H1 confirmed** (DTAG→GTT boundary inflates at peak, controls clean):
  supports the argument that Telekom under-provisions its transit handoff
  while keeping paid-peering leverage. Strongest commercial claim.
- **H2 confirmed**: weaker direct claim against Telekom, but the
  counterargument stands — Telekom *chose* the indirect GTT path; the report
  shows control ISPs reaching the same CAP cleanly via other interconnects.
- **H3 confirmed**: a GTT-internal issue; expect it to also affect non-Telekom
  GTT customers — verifiable by adding probes from other GTT-fed eyeballs.
- **H4 confirmed**: Cloudflare-side steering; raise with the CAP, though
  steering is itself often a *response* to congested ingress capacity from
  AS3320 — combine with the H1/H2 boundary data before assigning blame.
- **Nothing at peak**: the forward path is clean — investigate the reverse
  path (return traffic enters Telekom via its own paid-peering edges; run the
  mirror campaign from anchors/probes near the CAPs towards Telekom probes),
  IPv6 vs IPv4 differences, or in-home factors.

Caveats: ICMP hop RTTs can be inflated by router control-plane rate-limiting
(only *persistent, peak-correlated* step-ups that carry through to destination
RTT/loss count); MPLS within GTT can hide internal hops (biasing H3 towards
H2); reverse-path asymmetry means a step-up at hop N can be caused by the
*return* path of that hop's ICMP reply.
