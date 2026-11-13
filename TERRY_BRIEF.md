# Build Brief — Netpulse

**Agent:** Terry
**Repo:** `netpulse`
**Mode:** Greenfield scaffold from a single-file prototype

---

## Status — README + branding shipped

**Started:** 2026-08-18 · **Branch:** `claude/netpulse-status-e3f73s` · **Phase:** scaffolding

Repo baseline established. The prototype and its systemd unit are checked in as the
starting point for the split described in §3. §4.1 is done: the monolith lives in
`netpulse/` as eleven modules along the class seams described below, with every
`netmon` → `netpulse` rename from Ground rule #1 applied (paths, chain name, logger
name, CLI help/epilog, default filenames). §4.2 is done: `netpulse.conf`
(TOML) loading with `dataclass defaults -> config file -> CLI flags` precedence.
§4.3 is done: `--version` and `--export json|csv` round out the CLI surface listed
in the brief. §4.4 is done: a new `capabilities.py` detects root / raw-socket /
iptables availability at startup and disables just the affected subsystem instead
of crashing or degrading silently. §4.5 is done:
`packaging/netpulse.service` carries the prototype unit forward with every path
reconciled to the new defaults. §4.6 is now done too: 37 pytest tests across
config/DB/OUI/presence/export, which caught and fixed a real bug that had been
silently swallowed since the original prototype (see below). §4.7 is now done
too: `.github/workflows/ci.yml` runs ruff + pytest, unprivileged, across
Python 3.9-3.12. §5 is now done too: a full README with an SVG logo/banner, a
colorized terminal startup banner, and Skymind Automation branding throughout
(requested directly, off the standard §4 task list).

**Baseline facts (from the prototype):**

- `prototype-netmon.py` — 2999 lines, single file. Clean class seams already present:
  `Config` (23 keys), `Database`, `ARPDiscoverer`, `StateMonitor`, `BandwidthMonitor`,
  `Exporter`, `NetMonDaemon`, plus network utils, `lookup_vendor`, `setup_logging`,
  `daemonize`, and `run_live_display`.
- `OUI_VENDORS` — ~1900 embedded MAC-prefix entries. Lifts cleanly into `oui.py`
  (verified: 1909 entries in, 1909 out).
- `prototype-netmon.service` — Type=forking unit, hardened
  (`NoNewPrivileges`, `CapabilityBoundingSet`, `PrivateTmp`); paths still `netmon.*`,
  renamed and reconciled in `packaging/netpulse.service` (§4.5).

**Package split notes:**

- `parse_args()` was mis-indented as a method of `NetMonDaemon` in the prototype (no
  `self`, called bare in `main()`) — a copy-paste artifact, not intentional nesting.
  Dedented it to a module-level function in `cli.py`; this is the only way the
  original code could have actually run.
- `NetMonDaemon` → `NetpulseDaemon`; logger name `"netmon"` → `"netpulse"`.
- Added a minimal `pyproject.toml` (console_script `netpulse = netpulse.cli:main`)
  and a stub `README.md` so the package installs; both get filled in properly under
  §5 / later packaging tasks.
- Verified: every module parses, the full import graph resolves (checked against
  stubbed `scapy`/`psutil`/`netifaces` since this container can't build `netifaces`),
  `netpulse.config.Config()` produces the renamed default paths, and
  `netpulse.cli.parse_args(["--help"])` renders correctly.

**Config file loading notes (§4.2):**

- TOML, flat (no `[section]` tables — the whole `Config` is a flat key set, so no
  nesting is needed). Loaded with stdlib `tomllib` on 3.11+; on 3.9/3.10 (where
  `tomllib` doesn't exist and adding a third-party TOML dep isn't stdlib) a small
  subset parser in `config.py` (`_parse_simple_toml`) covers the same flat
  string/int/float/bool/string-array grammar. Verified both parsers produce
  identical output on `packaging/netpulse.conf.example`.
- Precedence implemented as three pure functions in `config.py` —
  `apply_config_file`, `apply_cli_overrides`, composed by `resolve_config()` — each
  returning a new `Config` rather than mutating in place.
- To make "CLI flag not passed" distinguishable from "CLI flag passed with a
  falsy/default-looking value," every `argparse` option that maps to a `Config`
  field now defaults to `None` (including the `store_true` flags, via an explicit
  `default=None`) instead of hardcoding the `Config` default inline. `cli.py`'s
  `cli_overrides_from_args()` does the argparse-name → Config-field mapping,
  including inverting `--no-iptables` into `use_iptables`.
- Added `--config PATH` (default `/etc/netpulse/netpulse.conf`) — pulled forward
  from §4.3's flag list since file-loading is unreachable without it.
- An unknown key in the config file raises `ValueError` and `main()` exits(1) with
  a `[!] Config error: ...` message rather than silently ignoring typos.
- Shipped `packaging/netpulse.conf.example` with all 23 `Config` keys, each
  commented; verified it round-trips through `resolve_config` unchanged (it's the
  defaults) and that both TOML backends parse it identically.
- Config file loading is not sandboxed against a malicious file beyond type
  parsing — same trust level as any other local config a root daemon reads.

**CLI polish notes (§4.3):**

- `run` (foreground) was already the implicit default with no flags — didn't add a
  redundant subcommand for it, just spelled it out as the first example in
  `--help`'s epilog. `--daemonize`, `--live`, and `--config` already landed with
  §4.2.
- `--version` uses argparse's built-in `action="version"`, reading
  `netpulse.__version__` — `netpulse --version` prints `netpulse 1.0.0` and exits 0.
- `--export {json,csv}` exports the *already-persisted* device DB and exits,
  deliberately not running a new ARP sweep first (unlike `--snapshot`, which does)
  — it's meant for pulling a report from a daemon that's already running as a
  service, so it only needs `Database` + `Exporter`, not the full `NetpulseDaemon`
  (no interface/subnet resolution, no root required). Verified end-to-end against
  a real SQLite DB: both `--export json` and `--export csv` write the expected
  file and print its path.

**Capability checks notes (§4.4):**

- New `netpulse/capabilities.py`: `has_root()` (`os.geteuid() == 0`), `has_raw_socket()`
  (opens an `AF_PACKET`/`SOCK_RAW` socket and closes it — the actual thing the ARP
  sweep needs on Linux, not just a root check; falls back to `has_root()` on
  non-Linux where `AF_PACKET` doesn't exist), and `has_iptables()`
  (`shutil.which("iptables")`). `check_capabilities()` runs all three once, logs a
  clear warning per missing capability, and returns a `Capabilities` dataclass —
  it never raises.
- `NetpulseDaemon.__init__` calls it right after resolving interface/subnet and
  before constructing any subsystem, then passes the verdict down instead of
  letting each subsystem discover the problem on its own:
  `ARPDiscoverer(..., enabled=capabilities.raw_socket)` and
  `BandwidthMonitor(..., use_iptables=(config.use_iptables and capabilities.iptables_binary))`.
  A disabled `ARPDiscoverer.sweep()` now returns `[]` immediately instead of
  attempting scapy and logging a warning every discovery cycle.
- Found and fixed a real bug while wiring this up: `BandwidthMonitor._init_iptables`
  never checked the return codes of the `iptables -N`/`-I` calls, so it set
  `iptables_initialized = True` unconditionally — a daemon running without root
  would silently believe per-device accounting was live when every `iptables`
  invocation had actually failed with "permission denied." Now it checks each
  return code and bails out (leaving `iptables_initialized = False`) with a
  specific warning naming which step failed.
- Verified end-to-end: `check_capabilities()` under simulated non-root/no-raw-socket/
  no-iptables (patched `os.geteuid`, `socket.socket`, `shutil.which`) reports all
  three correctly; a disabled `ARPDiscoverer` skips the scapy call entirely; a
  `BandwidthMonitor` pointed at a fake `iptables` binary that always exits 1 lands
  on `iptables_initialized == False` (confirming the bug fix) without raising; and
  a full `NetpulseDaemon` built under all-degraded capabilities completes
  init → discovery cycle → shutdown with zero crashes. Also re-checked the
  happy path (real root, real `iptables`, real raw socket in this container) still
  initializes normally.

**Systemd unit notes (§4.5):**

- `packaging/netpulse.service` carries `prototype-netmon.service` forward with every
  `netmon` → `netpulse` rename: description, binary path (`/usr/local/bin/netpulse`),
  `--pidfile`/`PIDFile` (`/var/run/netpulse.pid`), and `ReadWritePaths`
  (`/var/lib/netpulse`, `/var/log/netpulse.log`) — all reconciled against the same
  `Config` defaults from §4.1/§4.2, not re-guessed.
- Added `--config /etc/netpulse/netpulse.conf` to `ExecStart` explicitly (it's
  already the built-in default, but a service file naming its config path is more
  legible than relying on an implicit default someone has to go look up).
- Kept the hardening as-is (`NoNewPrivileges`, `CapabilityBoundingSet`,
  `AmbientCapabilities`, `PrivateTmp`) and added a comment tying the three
  capabilities directly to what §4.4's `capabilities.py` checks for: `CAP_NET_RAW`
  (raw sockets / ARP), `CAP_NET_ADMIN` (iptables), `CAP_NET_BROADCAST` (ARP
  broadcast) — so a deployment that runs this unqualified as non-root and grants
  only some of these capabilities gets the exact graceful per-subsystem
  degradation §4.4 built, documented at the point someone would actually edit it.
- `prototype-netmon.service` stays in the repo untouched as the baseline artifact
  (matches how `prototype-netmon.py` was kept as history, not deleted, in §4.1).
- Verified: `systemd-analyze verify packaging/netpulse.service` parses it cleanly —
  its only complaint is that `/usr/local/bin/netpulse` isn't installed in this
  container, which is expected since nothing has `pip install`ed the package yet.

**Tests notes (§4.6):**

- 37 pytest tests across `tests/test_config.py`, `test_db.py`, `test_oui.py`,
  `test_presence.py`, `test_export.py` — exactly the coverage areas the brief
  named. Deliberately did **not** add `test_discovery.py`, `test_bandwidth.py`,
  `test_daemon.py`, or `test_cli.py`: those modules import `scapy`/`psutil`/
  `netifaces`, and none of the five target areas need them, so the whole suite
  stays runnable without those (sometimes hard-to-build, e.g. `netifaces` failed
  to build in this exact container) third-party deps installed.
- `tests/conftest.py` inserts the repo root onto `sys.path` so `pytest` works from
  any cwd without an editable install.
- `test_presence.py` mocks `netpulse.presence.subprocess.run` — no real ping, no
  real network — and drives `StateMonitor.check_all()` by forcing `last_checks[mac]
  = 0` before each call rather than sleeping through real intervals, to assert the
  exponential-backoff sequence (10 → 20 → 40 → capped) directly.
- **Found and fixed a real, previously-silent bug while writing `test_db.py`**:
  `Database.upsert_device`'s existing-row lookup was
  `SELECT state, state_changed FROM devices WHERE mac = ?`, but the code a few
  lines later reads `row["first_seen"]` — a column that query never selected.
  Every upsert of an *already-known* device raised `IndexError: No item with that
  key`. It never crashed the daemon because `StateMonitor.check_all()` wraps each
  device's check in a bare `except Exception`, so it silently logged
  `"Error checking <mac>: No item with that key"` and skipped the rest of that
  device's cycle — including the backoff update, which is how it also surfaced as
  a second test failure (`check_all`'s backoff never advanced past its initial
  value, because the exception fired before the backoff math ran). This meant a
  device's `first_seen` was recomputed as "now" on every single check that
  otherwise would have hit the update path, and `state_history`/backoff tracking
  for already-known devices was effectively broken end to end. Fixed by adding
  `first_seen` to the `SELECT`; both tests pass with real assertions now, not
  just "doesn't crash."
- Added a `test` extra (`pip install .[test]`) and `[tool.pytest.ini_options]`
  (`testpaths = ["tests"]`) to `pyproject.toml` so bare `pytest` works without
  extra flags.
- Ran the full suite: `37 passed`.

**CI notes (§4.7):**

- `.github/workflows/ci.yml`: a `lint` job (`ruff check netpulse tests` on 3.12)
  and a `test` job (`pytest`, matrix over Python 3.9/3.10/3.11/3.12). Both trigger
  on push and pull_request. Didn't hardcode `branches: [main]` on the push trigger
  — this repo's actual default branch is `claude/terry-brief-update-6kw2nc`, not
  `main`, so that would have silently made push-triggered CI never run.
- The `test` job installs only `pytest`, deliberately not the package's own
  `scapy`/`psutil`/`netifaces` runtime dependencies — same reasoning as §4.6's
  test scope: nothing in `tests/` needs them, so the matrix isn't at the mercy of
  wheel availability for those three packages across four Python versions (this
  container, for instance, can't build `netifaces` at all). Verified by running
  the exact CI recipe locally in a throwaway venv with nothing but `pytest`
  installed: `37 passed`.
- Added an explicit unprivileged check as its own step
  (`if [ "$(id -u)" -eq 0 ]; then ... exit 1; fi`) so "tests must pass
  unprivileged" is enforced by CI itself, not just true by accident of how
  GitHub-hosted runners happen to be configured.
- Fixed 5 real `ruff`-caught issues in the process of wiring lint into CI (they'd
  have failed the first CI run otherwise): an unused `now` in
  `BandwidthMonitor.sample_per_device`, an f-string with no placeholders and a
  dead `log` variable in `cli.py`, a dead `last_discovery_check` variable in
  `NetpulseDaemon.run`'s hot loop, and a redundant local re-import of
  `IPv4Network` in `discovery.py` shadowing the already-imported module-level
  one. All cosmetic/dead-code, no behavior change; re-ran the full test suite
  and a manual daemon smoke test after to confirm.
- **Worth flagging**: while testing the CI recipe in a clean venv, `pip install
  ruff` pulled ruff 0.16.3, whose *default* lint rule selection is dramatically
  larger than 0.15.8's (71 findings vs. 5, adding pyupgrade/flake8-bandit/
  flake8-datetimez/pylint/isort categories neither version had opted into
  explicitly). An unpinned `ruff check` in CI would have started failing on a
  future `pip install ruff` picking up a newer default, for code that hadn't
  changed. Fixed by adding `[tool.ruff.lint] select = ["E4", "E7", "E9", "F"]`
  to `pyproject.toml` — ruff's own long-standing classic default — so lint
  behavior is pinned by config, not by whichever ruff version CI happens to
  install that day. Verified `ruff check` is clean under both 0.15.8 and 0.16.3
  with this config in place.
- Verified the actual CI recipe end-to-end in fresh venvs (not just the repo's
  ambient Python): `ruff check netpulse tests` → all checks passed; `pytest` →
  `37 passed`, using only `pytest`/`ruff` with none of the runtime deps
  installed.

**README / branding notes (§5, requested directly by the user):**

- New `netpulse/banner.py`: a colorized startup banner (pulse-wave line +
  letter-spaced "NETPULSE" wordmark + version + "◆ Skymind Automation" byline),
  printed to **stderr** — deliberately, so it never lands in `--snapshot`'s or
  `--export`'s stdout, both of which are meant to be piped/parsed. `--export`
  skips the banner entirely (cron/script use, not interactive). Color degrades
  automatically for `NO_COLOR`, `TERM=dumb`, or a non-tty stream — covered by
  5 new tests in `tests/test_banner.py` (42 total now). Verified by hand: a
  `--snapshot` run's stdout is still clean, parseable JSON with the banner
  showing up only on stderr.
- New `assets/logo.svg` (standalone icon) and `assets/banner.svg` (icon +
  wordmark lockup for the README header): a network-pulse mark — four LAN
  "device" nodes wired to a center hub, with an EKG-style pulse line running
  through it, cyan→violet gradient, Skymind Automation's purple accent on the
  byline. Hand-authored SVG (no image-gen tooling available in this
  environment) — verified well-formed as XML and, since `cairosvg` installs
  cleanly here, actually rendered to PNG and visually checked before writing
  it into the README rather than trusting the markup blind.
- Full `README.md` rewrite (was a one-line stub from §4.1): banner image up
  top, CI/Python/license/no-egress badges, one-line pitch, a terminal
  transcript combining the startup banner and the live-monitor table (the
  brief's "screenshot/ASCII of the live display" ask), features, requirements,
  install (pip + systemd), quickstart, a full 23-key configuration table
  (cross-checked against `Config`'s actual fields so nothing's stale or
  invented), the no-egress privacy note, export-format docs, uninstall, and
  license. Didn't link to `docs/configuration.md` since that file doesn't
  exist yet (separate, still-open item below) — a README linking its own
  missing file would be worse than the table standing alone.
- Added `authors = [{ name = "Skymind Automation" }]` to `pyproject.toml` so
  the branding shows up in package metadata too, not just the README.
- Added a new `LICENSE` (MIT, copyright Skymind Automation) — referenced by
  the README's license section and by `pyproject.toml`'s `license` field, but
  hadn't actually been created yet; §3's deliverables list included it even
  though it wasn't its own checklist line.
- Verified after all of the above: `pytest` → 42 passed, `ruff check` → clean,
  and a manual CLI run confirming banner-on-stderr / JSON-on-stdout
  separation and that `--export` suppresses the banner as intended.

**Progress log:**

- [x] Repo baseline committed (prototype + service unit + this brief)
- [x] §4.1 Package split into `netpulse/` modules
- [x] §4.2 Config file loading (TOML, stdlib) + precedence
- [x] §4.3 CLI polish (`run`, `--daemonize`, `--live`, `--export`, `--config`, `--version`)
- [x] §4.4 Graceful capability checks (root / iptables / raw socket)
- [x] §4.5 `packaging/netpulse.service` renamed + path-reconciled
- [x] §4.6 Tests (config precedence, OUI, DB, presence backoff, export)
- [x] §4.7 CI (ruff + pytest, 3.9–3.12, unprivileged)
- [x] §5 README (+ SVG logo/banner + terminal startup banner + Skymind Automation branding)
- [x] packaging/netpulse.conf.example
- [x] LICENSE (MIT, Skymind Automation)
- [ ] Docs (docs/configuration.md), CHANGELOG

---

## 0. Who you are

You are **Terry** — an autonomous build engineer. You take a rough prototype and turn it
into a shippable, well-structured, well-documented project. You are opinionated about
clean layout, small honest commits, and READMEs a stranger can actually follow. You do
not narrate fluff; you ship.

**Your signature.** This project does *not* use the generic "Generated with Claude Code"
footer. You sign your own work. On every commit, PR body, and release note you author,
close with this footer verbatim (blank line, rule, then the italic line):

```
---
_Forged by Terry ⚡ — autonomous build agent_
```

Nothing else — no model names, no vendor tags, no other attribution lines. That footer is
your identity across the repo. Keep it identical everywhere so it reads as one hand.

---

## 1. What Netpulse is

Netpulse is a **self-hosted LAN monitoring daemon**. On the network it runs on, it:

- **Discovers devices** — passive/low-footprint ARP sweeps + MAC vendor (OUI) lookup
- **Tracks presence** — adaptive ICMP online/offline detection with exponential backoff
  for hosts that stay offline (this heartbeat is the core loop — hence the name)
- **Accounts bandwidth** — per-device via iptables conntrack, per-interface via `/proc/net/dev`
- **Persists & exports** — SQLite storage with JSON/CSV snapshots and retention pruning
- **Runs as a daemon** — low I/O priority, systemd unit, PID file, clean shutdown

Everything stays on the LAN it monitors. There is **no external egress, no telemetry, no
phone-home** — and it must stay that way. This is admin tooling for one's own network
(think a self-hosted "who's on my WiFi" + bandwidth monitor). Requires root for raw
sockets (ARP/ICMP) and iptables accounting.

The starting point is a ~3000-line single file (`netmon.py`) plus a `netmon.service`
systemd unit. Your job is to rename it to **Netpulse** and turn it into a real project.

---

## 2. Ground rules

1. **Rename everything** `netmon` → `netpulse` (module names, CLI, paths, chain names,
   DB/log/pid file defaults, service unit, docstrings). Default paths become
   `/var/lib/netpulse`, `/var/log/netpulse.log`, `/var/run/netpulse.pid`, iptables chain
   `NETPULSE_INPUT`, systemd unit `netpulse.service`, binary `netpulse`.
2. **No new network destinations.** Preserve the no-egress guarantee. The only outbound
   traffic is ARP/ICMP on the local segment. If you add an optional OUI refresh from
   `standards-oui.ieee.org`, it must be **opt-in, off by default, and clearly documented**.
3. **Preserve behavior.** This is a refactor + packaging job, not a rewrite. Keep the
   discovery/presence/bandwidth/export logic intact; restructure it, don't reinvent it.
4. **Root-only features degrade gracefully.** If iptables or raw sockets aren't available,
   log a clear warning and keep the rest running — don't crash.

---

## 3. Deliverables — target repo layout

```
netpulse/
├── netpulse/
│   ├── __init__.py            # __version__ = "1.0.0"
│   ├── __main__.py            # `python -m netpulse`
│   ├── config.py             # Config dataclass + file loading (see §4)
│   ├── db.py                 # Database
│   ├── discovery.py          # ARPDiscoverer + network utils
│   ├── presence.py           # StateMonitor (adaptive ICMP)
│   ├── bandwidth.py          # BandwidthMonitor (iptables + /proc/net/dev)
│   ├── export.py             # Exporter (JSON/CSV)
│   ├── daemon.py             # NetpulseDaemon, daemonize, signal handling
│   ├── cli.py                # argparse entrypoint + live display
│   ├── logging_setup.py      # setup_logging
│   └── oui.py                # OUI_VENDORS table + lookup_vendor
├── tests/
│   ├── test_config.py
│   ├── test_db.py
│   ├── test_oui.py
│   ├── test_presence.py      # mock ping — no real network
│   └── test_export.py
├── packaging/
│   ├── netpulse.service      # renamed + corrected systemd unit
│   └── netpulse.conf.example # sample config file
├── docs/
│   └── configuration.md      # every config key documented
├── README.md
├── LICENSE                   # MIT unless told otherwise
├── CHANGELOG.md              # start at 1.0.0
├── pyproject.toml            # PEP 621, console_script `netpulse = netpulse.cli:main`
├── requirements.txt          # scapy, psutil, netifaces
├── .gitignore                # Python + *.db, *.log, exports/, __pycache__
└── .github/workflows/ci.yml  # lint + tests on 3.9–3.12
```

Split the monolith along the class boundaries already in the prototype — the seams are
clean (`Config`, `Database`, `ARPDiscoverer`, `StateMonitor`, `BandwidthMonitor`,
`Exporter`, `NetMonDaemon`, network utils, OUI table). Keep public class/method names
except the `NetMon*` → `Netpulse*` renames.

---

## 4. Specific engineering tasks

1. **Package split** — break `netmon.py` into the modules above. No behavior change.
2. **Config file support** — the prototype only has a `Config` dataclass with defaults.
   Add loading from a file (`/etc/netpulse/netpulse.conf`, TOML or INI — pick one, keep
   it stdlib: `tomllib` on 3.11+, else fall back). CLI flags override file values which
   override dataclass defaults. Ship `packaging/netpulse.conf.example` with every key.
3. **CLI polish** — subcommands or flags for: `run` (foreground), `--daemonize`,
   `--live` (the curses/live display), `--export json|csv`, `--config PATH`,
   `--version`. `netpulse --version` reads `__version__`.
4. **Graceful capability checks** — detect missing root / iptables / raw-socket
   permission at startup; warn and disable just the affected subsystem.
5. **Fix the systemd unit** — carry over `packaging/netpulse.service`; make `ReadWritePaths`
   and `PIDFile` consistent with the new default paths; keep the hardening
   (`NoNewPrivileges`, `CapabilityBoundingSet`, `PrivateTmp`).
6. **Tests** — pytest, no real network. Mock `subprocess`/scapy where needed. Cover:
   config precedence, OUI lookup (incl. unknown → "Unknown"), DB upsert/retention,
   presence backoff math, JSON/CSV export shape. Aim for the logic, not coverage theater.
7. **CI** — GitHub Actions: `ruff` (or `flake8`) + `pytest` on Python 3.9–3.12. Don't
   require root in CI — tests must pass unprivileged.

---

## 5. README (make it good)

Sections: what it is · screenshot/ASCII of the live display · features · requirements
(root, Linux, deps) · install (pip + systemd) · quickstart · configuration table ·
data & privacy note (no egress) · export formats · uninstall · license. Lead with a
one-line pitch. Assume the reader has never seen the tool.

---

## 6. Quality bar & git workflow

- Small, focused commits with imperative subjects (`Split bandwidth monitor into module`,
  not `updates`). Each commit builds and tests green.
- Every commit and the PR body end with your signature footer from §0.
- Work on the branch you're told to use; push there. Open a PR **only if asked**.
- When done, post a short summary: what shipped, how to run it, what's deliberately left
  for a follow-up.

Ship it, Terry. ⚡
