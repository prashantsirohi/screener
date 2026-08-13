"""
Command surface.

    screener doctor              environment and connectivity check
    screener migrate             apply pending migrations
    screener migrate --verify    report drift without applying
    screener status              watermarks, freshness, retry queue depth
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .config import load_settings
from .db.connection import Database
from .db import migrate as mig
from .logging_setup import setup_logging

OK, WARN, BAD = "[ ok ]", "[warn]", "[fail]"


def _cmd_doctor(args) -> int:
    st = load_settings(domain=args.domain)
    st.paths.ensure()
    problems = 0

    print(f"market-screener {__version__}")
    print(f"  python            {sys.version.split()[0]}")
    print(f"  project root      {st.project_root}")
    print(f"  domain            {st.domain}")
    print(f"  data root         {st.paths.data_root}")
    print(f"  reports root      {st.paths.reports_root}")
    print(f"  logs root         {st.paths.logs_root}")
    print(f"  price basis       {st.price_basis}"
          + ("  (price return, exchange-sourced)" if st.price_basis == "split_bonus"
             else "  (total return, aggregator-sourced)"))
    _gate = st.screen.technical_gate_exclude_stages
    _rs = st.screen.technical_gate_min_rs_13w
    if _gate or _rs is not None:
        print(f"  technical gate    excludes {', '.join(_gate) or 'no stages'}"
              + (f"; 13w RS floor {_rs:+.1f}%" if _rs is not None else ""))
    else:
        print("  technical gate    off  (stage only nudges the score)")
    print(f"  config hash       {st.config_hash()}")

    db = Database(st.pg)
    ver = db.server_version()
    if ver:
        print(f"  {OK} postgres        {ver.split(',')[0]}")
    else:
        print(f"  {BAD} postgres        cannot reach {st.pg.host}:{st.pg.port} as {st.pg.user}")
        problems += 1

    if ver:
        exists = db.database_exists()
        if exists:
            print(f"  {OK} database        {st.pg.database} exists")
            if db.ping():
                try:
                    head = mig.head(db)
                    pending = [p for p in mig.verify(db) if p.endswith("pending")]
                    drift = [p for p in mig.verify(db) if not p.endswith("pending")]
                    print(f"  {OK} migrations      head={head or 'none'}, "
                          f"{len(pending)} pending")
                    for d in drift:
                        print(f"  {BAD} migration drift {d}")
                        problems += 1
                except Exception as exc:
                    print(f"  {BAD} migrations      {exc}")
                    problems += 1
        else:
            print(f"  {WARN} database        {st.pg.database} does not exist "
                  f"(run `screener migrate` to create it)")

    try:
        import duckdb
        con = duckdb.connect()
        print(f"  {OK} duckdb          {duckdb.__version__}")
        if st.analytics_mode == "attach":
            try:
                con.execute("INSTALL postgres; LOAD postgres;")
                print(f"  {OK} duckdb postgres extension loaded (mode=attach)")
            except Exception as exc:
                print(f"  {BAD} duckdb postgres extension unavailable: {exc}")
                print("         set SCREENER_ANALYTICS_MODE=parquet to fall back")
                problems += 1
        else:
            print(f"  {OK} analytics mode  parquet (postgres extension not required)")
        con.close()
    except ImportError:
        print(f"  {BAD} duckdb          not installed")
        problems += 1

    print(f"  {OK} session cookie  {'set' if st.screener_session_cookie else 'not set (optional)'}")
    print("\n" + ("all checks passed" if problems == 0 else f"{problems} problem(s) found"))
    return 0 if problems == 0 else 1


def _cmd_migrate(args) -> int:
    st = load_settings(domain=args.domain)
    setup_logging(st.paths.logs_root, st.log_level)
    db = Database(st.pg)

    if args.verify:
        if not db.database_exists():
            print(f"database {st.pg.database} does not exist")
            return 1
        problems = mig.verify(db)
        if not problems:
            print(f"clean; head = {mig.head(db)}")
            return 0
        for p in problems:
            print(f"  {p}")
        # Pending migrations are expected before an upgrade; drift is not.
        return 0 if all(p.endswith("pending") for p in problems) else 1

    created = db.create_database()
    if created:
        print(f"created database {st.pg.database}")
    applied = mig.upgrade(db, target=args.target)
    if applied:
        for a in applied:
            print(f"  applied {a}")
    else:
        print("nothing to apply")
    print(f"head = {mig.head(db)}")
    return 0


def _cmd_status(args) -> int:
    st = load_settings(domain=args.domain)
    db = Database(st.pg)
    if not db.database_exists() or not db.ping():
        print("database unavailable; run `screener doctor`")
        return 1

    def table_count(t: str) -> str:
        try:
            return f"{db.fetch_value(f'SELECT count(*) AS c FROM market.{t}'):,}"
        except Exception:
            return "-"

    print("row counts")
    for t in ("security", "price_daily", "weekly_bar", "screener_page_raw",
              "screener_fact", "announcement", "screen_run", "phase1_universe"):
        print(f"  {t:<22} {table_count(t):>12}")

    marks = db.fetch_all(
        "SELECT source, scope, watermark, last_run_at, last_status, rows_written "
        "FROM market.sync_watermark ORDER BY source, scope")
    print("\nwatermarks")
    if not marks:
        print("  (none yet)")
    for m in marks:
        print(f"  {m['source']:<22} {str(m['scope']):<12} {str(m['watermark']):<22} "
              f"{str(m['last_status'])}")

    try:
        q = db.fetch_all(
            "SELECT state, count(*) AS n FROM market.fetch_retry_queue GROUP BY state")
        print("\nretry queue")
        print("  (empty)" if not q else "")
        for r in q:
            print(f"  {r['state']:<12} {r['n']}")
        blanks = db.fetch_value(
            "SELECT count(*) AS c FROM market.screener_page_raw WHERE is_blank")
        print(f"  blank pages quarantined: {blanks}")
    except Exception:
        pass

    # ---- reconciliation, with its trend --------------------------------------
    from .pipeline import health

    snaps = health.reconciliation_snapshots(db)
    if snaps:
        cur = health._verdicts(db, snaps[0])
        prev = health._verdicts(db, snaps[1]) if len(snaps) > 1 else {}
        total = sum(cur.values())
        print(f"\nprice-source reconciliation (as of {snaps[0]}"
              + (f", previous {snaps[1]}" if len(snaps) > 1 else "") + ")")
        for verdict in ("agree", "drift", "insufficient", "disagree", "missed_action"):
            n = cur.get(verdict)
            if n is None:
                continue
            if prev:
                d = n - prev.get(verdict, 0)
                delta = f"{d:+d}" if d else "   ="
            else:
                delta = ""
            flag = "  <- unfound corporate actions" if verdict == "missed_action" else ""
            print(f"  {verdict:<16} {n:>6} {delta:>6}{flag}")
        if total:
            missed = cur.get("missed_action", 0) + cur.get("disagree", 0)
            print(f"  {'':<16} {missed / total * 100:>5.1f}% of {total} compared")

        worst = health.worst_unreconciled(db, 5)
        if worst:
            print("\n  largest unexplained steps")
            for w in worst:
                print(f"    {w['symbol']:<14} {str(w['verdict']):<14} "
                      f"step {w['step_pct']}%  over {w['weeks_compared']} weeks")

    # ---- alerts ---------------------------------------------------------------
    checks = health.run_all(db)
    firing = [c for c in checks if c.level in ("warn", "alert")]
    print("\nalerts")
    if not firing:
        print("  none - everything within thresholds")
    for c in sorted(firing, key=lambda x: 0 if x.level == "alert" else 1):
        print(f"  [{c.level.upper():<5}] {c.name}: {c.message}")
        if c.detail:
            print(f"          {c.detail}")
    if args.verbose:
        for c in checks:
            if c.level == "ok":
                print(f"  [ OK   ] {c.name}: {c.message}")

    if args.strict and firing:
        return 1
    return 0


def _cmd_import_legacy(args) -> int:
    from .ingest import legacy_import

    st = load_settings(domain=args.domain)
    setup_logging(st.paths.logs_root, st.log_level)
    db = Database(st.pg)
    if not db.database_exists() or not db.ping():
        print("database unavailable; run `screener migrate` first")
        return 1

    steps = args.steps.split(",") if args.steps else None
    if steps:
        unknown = [s for s in steps if s not in legacy_import.STEPS]
        if unknown:
            print(f"unknown step(s): {unknown}; valid: {list(legacy_import.STEPS)}")
            return 2

    result = legacy_import.run(db, st.paths.data_root, steps)
    print("\nimport summary")
    for step, info in result.items():
        print(f"  {step}")
        for k, v in (info or {}).items():
            print(f"      {k:<20} {v}")
    return 0


def _open_db(args):
    """Load settings and return (settings, db) or (settings, None) if unusable."""
    st = load_settings(domain=args.domain)
    setup_logging(st.paths.logs_root, st.log_level)
    db = Database(st.pg)
    if not db.database_exists() or not db.ping():
        print("database unavailable; run `screener migrate` first")
        return st, None
    return st, db


def _cmd_sync(args) -> int:
    st, db = _open_db(args)
    if db is None:
        return 1

    results: dict = {}
    if args.source in ("prices", "all"):
        from .ingest import price_sync
        results["prices"] = price_sync.sync(
            db, backfill_days=args.backfill, max_days=args.max_days,
            force=args.force)

    if args.source in ("documents", "all"):
        from .ingest import document_sync
        results["documents"] = document_sync.sync(st, db, limit=args.max_days)

    if args.source in ("announcements", "all"):
        from .ingest import announcement_sync
        results["announcements"] = announcement_sync.sync(
            db, backfill_days=args.backfill, max_windows=args.max_days)

    if args.source in ("indices", "all"):
        from .ingest import index_sync
        results["indices"] = index_sync.sync(
            db, backfill_days=args.backfill, max_days=args.max_days,
            force=args.force)

    if args.source in ("shares", "all"):
        from .ingest import shares_sync
        results["shares"] = shares_sync.sync(
            db, limit=args.max_days, only_missing=not args.force)

    if args.source in ("fundamentals", "all"):
        from .ingest import fundamentals_sync
        if args.retry_queue:
            results["fundamentals_retry"] = fundamentals_sync.drain_retry_queue(
                st, db, limit=args.max_days or 25)
        else:
            results["fundamentals_refresh"] = fundamentals_sync.refresh_stale(
                st, db, limit=args.max_days or 25)

    print("\nsync summary")
    for name, info in results.items():
        print(f"  {name}")
        for k, v in (info or {}).items():
            print(f"      {k:<18} {v}")
    return 0


def _cmd_derive(args) -> int:
    from datetime import date as _date
    from .ingest import price_derive

    st, db = _open_db(args)
    if db is None:
        return 1
    as_of = _date.fromisoformat(args.as_of) if args.as_of else _date.today()

    out: dict = {}
    if args.what in ("actions", "all"):
        from .ingest import corp_action_sync
        out["actions"] = corp_action_sync.infer(st, db, as_of)
    if args.what in ("actions-divergence", "all"):
        from .ingest import corp_action_sync
        out["actions_divergence"] = corp_action_sync.from_divergence(st, db, as_of)
    if args.what in ("adjusted", "all"):
        out["adjusted"] = price_derive.rebuild_adjusted(st, db, as_of)
    if args.what in ("weekly", "all"):
        out["weekly"] = price_derive.rebuild_weekly(st, db, as_of)
    if args.what in ("reconcile", "all"):
        out["reconcile"] = price_derive.reconcile_sources(st, db, as_of)
        out["source_choice"] = price_derive.report_source_choice(db)

    print(f"\nderive summary (as_of={as_of})")
    for name, info in out.items():
        print(f"  {name}")
        for k, v in (info or {}).items():
            print(f"      {k:<22} {v}")
    return 0


def _cmd_screen(args) -> int:
    from datetime import date as _date
    from .pipeline import orchestrator

    st, db = _open_db(args)
    if db is None:
        return 1
    as_of = _date.fromisoformat(args.as_of) if args.as_of else _date.today()
    out = orchestrator.run_phase1(
        st, db, as_of=as_of, force=args.force, resume=args.resume,
        stages=args.stages.split(",") if args.stages else None)

    print(f"\nrun {out['run_id']} -> {out['status']}")
    print(f"  as_of      {out.get('as_of')}")
    print(f"  output     {out.get('output_dir')}")
    for k, v in (out.get("counts") or {}).items():
        print(f"  {k:<10} {v}")
    print("\nstages")
    for stage, info in out["stages"].items():
        detail = ", ".join(f"{k}={v}" for k, v in info.items() if k != "status")
        print(f"  {stage:<22} {info['status']:<9} {detail}")
    return 0 if out["status"] == "complete" else 1


def _cmd_runs(args) -> int:
    from .pipeline import runs as runs_mod

    st, db = _open_db(args)
    if db is None:
        return 1

    if args.runs_command == "list":
        rows = runs_mod.list_runs(db, args.limit)
        if not rows:
            print("no runs recorded")
            return 0
        print(f"{'run_id':<28} {'status':<10} {'as_of':<12} "
              f"{'eval':>6} {'elig':>6} {'sel':>5} {'qc!':>4}  started")
        for r in rows:
            print(f"{r['run_id']:<28} {r['status']:<10} {str(r['as_of_date']):<12} "
                  f"{r['evaluated'] or 0:>6} {r['eligible'] or 0:>6} "
                  f"{r['selected'] or 0:>5} {r['qc_failed']:>4}  "
                  f"{r['started_at']:%Y-%m-%d %H:%M}")
        return 0

    if args.runs_command == "show":
        info = runs_mod.show_run(db, args.run_id)
        if not info:
            print(f"no such run: {args.run_id}")
            return 1
        r = info["run"]
        print(f"run {r['run_id']}  [{r['status']}]")
        print(f"  as_of {r['as_of_date']}   basis via config_hash {r['config_hash']}")
        print(f"  counts {r['counts']}")
        print("\nstages")
        for s in info["stages"]:
            extra = s["skip_reason"] or s["error"] or ""
            print(f"  {s['stage']:<22} {s['status']:<9} "
                  f"{str(s['seconds'] or '-'):>5}s  out={s['rows_out'] or '-':<7} {extra}")
        print("\nartifacts")
        for a in info["artifacts"]:
            print(f"  {a['artifact_name']:<28} rows={str(a['row_count'] or '-'):>7} "
                  f"{a['bytes']:>9,}B  {a['sha']}")
        failed = [q for q in info["qc"] if not q["passed"]]
        print(f"\nqc: {len(info['qc']) - len(failed)}/{len(info['qc'])} passed")
        for q in failed:
            print(f"  FAIL {q['check_id']} {q['check_name']} - {q['detail']}")
        print("\neligible by archetype")
        for a in info["archetypes"]:
            print(f"  {str(a['primary_archetype']):<38} {a['n']:>5}")
        return 0

    if args.runs_command == "diff":
        d = runs_mod.diff_runs(db, args.base, args.other)
        if "error" in d:
            print(d["error"])
            return 1
        print(f"diff {d['base']}  ->  {d['other']}\n")
        u = d["universe"]
        print(f"universe    in both {u['in_both']}, only base {u['only_in_base']}, "
              f"only other {u['only_in_other']}")
        e = d["eligibility"]
        print(f"eligible    {e['base']} -> {e['other']}  "
              f"(+{e['gained_count']} / -{e['lost_count']})")
        c = d["candidates"]
        print(f"candidates  {c['base']} -> {c['other']}  "
              f"(entered {c['entered_count']}, left {c['left_count']}, "
              f"unchanged {c['unchanged']})")
        if c["entered"]:
            print(f"  entered: {', '.join(c['entered'][:15])}")
        if c["left"]:
            print(f"  left   : {', '.join(c['left'][:15])}")
        if d["changed_fields"]:
            print("\nfields changed for companies in both runs")
            for col, n in sorted(d["changed_fields"].items(), key=lambda x: -x[1]):
                ex = d["examples"].get(col, [])
                s = "; ".join(f"{x['symbol']}: {x['from']} -> {x['to']}" for x in ex)
                print(f"  {col:<32} {n:>5}   {s[:90]}")
        else:
            print("\nno field changes for companies present in both runs")
        return 0

    if args.runs_command == "prune":
        print(runs_mod.prune_runs(db, args.keep))
        return 0
    return 1


def _cmd_export_parquet(args) -> int:
    from .analytics.duck import export_parquet

    st, db = _open_db(args)
    if db is None:
        return 1
    out = export_parquet(st, db.execute)
    print(f"exported to {st.paths.parquet_dir}")
    for table, n in out.items():
        print(f"  {table:<28} {n:>10,} rows")
    print("\nset SCREENER_ANALYTICS_MODE=parquet to read these instead of Postgres")
    return 0


def _cmd_classify(args) -> int:
    from .ingest import classify_events

    st, db = _open_db(args)
    if db is None:
        return 1
    relink = classify_events.relink_announcements(db)
    print("relink summary")
    for k, v in relink.items():
        print(f"  {k:<26} {v}")
    out = classify_events.classify_all(db)
    print("\nclassification summary")
    for k, v in out.items():
        print(f"  {k:<18} {v}")
    if args.diff:
        d = classify_events.diff_versions(db)
        print("\nv1 categories")
        for k, v in sorted(d["v1"].items(), key=lambda x: -x[1]):
            print(f"  {k:<38} {v:>7,}")
        print("\nv2 categories (top 15)")
        for k, v in sorted(d["v2"].items(), key=lambda x: -x[1])[:15]:
            print(f"  {k:<38} {v:>7,}")
    return 0


def _cmd_rebuild_facts(args) -> int:
    from .ingest import fundamentals_sync

    st, db = _open_db(args)
    if db is None:
        return 1
    out = fundamentals_sync.rebuild_facts_from_payloads(db)
    print("\nrebuild summary")
    for k, v in out.items():
        print(f"  {k:<18} {v}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="screener",
                                description="Persistent Indian-equity screening system")
    p.add_argument("--version", action="version", version=f"market-screener {__version__}")
    p.add_argument("--domain", choices=("operational", "research"), default="operational",
                   help="research keeps a parallel data/reports tree")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="environment and connectivity check")
    d.set_defaults(func=_cmd_doctor)

    m = sub.add_parser("migrate", help="apply pending schema migrations")
    m.add_argument("--verify", action="store_true", help="report drift without applying")
    m.add_argument("--target", help="stop at this migration version, e.g. 0005")
    m.set_defaults(func=_cmd_migrate)

    s = sub.add_parser("status",
                       help="row counts, watermarks, reconciliation trend, alerts")
    s.add_argument("--strict", action="store_true",
                   help="exit non-zero if any alert is firing (for scheduled runs)")
    s.add_argument("--verbose", action="store_true",
                   help="also show checks that are passing")
    s.set_defaults(func=_cmd_status)

    sy = sub.add_parser("sync", help="incrementally refresh a data source")
    sy.add_argument("--source", default="all",
                    choices=("all", "prices", "indices", "announcements",
                             "documents", "shares", "fundamentals"),
                    help="which collector to run")
    sy.add_argument("--retry-queue", action="store_true",
                    help="fundamentals: work the quarantined blank-page queue")
    sy.add_argument("--backfill", type=int,
                    help="ignore the watermark and start N calendar days back")
    sy.add_argument("--max-days", type=int,
                    help="cap the number of items fetched this run")
    sy.add_argument("--force", action="store_true",
                    help="refetch items that are already present")
    sy.set_defaults(func=_cmd_sync)

    dv = sub.add_parser("derive", help="rebuild adjusted prices and weekly bars")
    dv.add_argument("--what", default="all",
                    choices=("all", "actions", "actions-divergence", "adjusted",
                             "weekly", "reconcile"))
    dv.add_argument("--as-of", help="YYYY-MM-DD; defaults to today")
    dv.set_defaults(func=_cmd_derive)

    sc = sub.add_parser("screen", help="run the Phase 1 screen and emit the hand-off")
    sc.add_argument("--as-of", help="YYYY-MM-DD; defaults to today")
    sc.add_argument("--force", action="store_true",
                    help="run every stage even if its inputs are unchanged")
    sc.add_argument("--resume", action="store_true",
                    help="continue the most recent unfinished run for this as_of")
    sc.add_argument("--stages", help="comma-separated subset of stage names")
    sc.set_defaults(func=_cmd_screen)

    rn = sub.add_parser("runs", help="inspect and compare screening runs")
    rsub = rn.add_subparsers(dest="runs_command", required=True)
    rl = rsub.add_parser("list", help="recent runs with their counts")
    rl.add_argument("--limit", type=int, default=20)
    rl.set_defaults(func=_cmd_runs)
    rs = rsub.add_parser("show", help="stages, artifacts and QC for one run")
    rs.add_argument("run_id")
    rs.set_defaults(func=_cmd_runs)
    rd = rsub.add_parser("diff", help="what changed between two runs, and where")
    rd.add_argument("base")
    rd.add_argument("other")
    rd.set_defaults(func=_cmd_runs)
    rp = rsub.add_parser("prune", help="delete all but the most recent N runs")
    rp.add_argument("--keep", type=int, default=10)
    rp.set_defaults(func=_cmd_runs)

    ep = sub.add_parser("export-parquet",
                        help="materialise the source tables for offline analytics")
    ep.set_defaults(func=_cmd_export_parquet)

    cl = sub.add_parser("classify-events",
                        help="classify stored announcements (v1 and v2 taxonomies)")
    cl.add_argument("--diff", action="store_true",
                    help="print the v1 vs v2 category breakdown")
    cl.set_defaults(func=_cmd_classify)

    rf = sub.add_parser("rebuild-facts",
                        help="re-explode facts from retained page payloads "
                             "(replays a parser fix without re-fetching)")
    rf.set_defaults(func=_cmd_rebuild_facts)

    il = sub.add_parser("import-legacy-cache",
                        help="load the existing data/ JSON caches without re-scraping")
    il.add_argument("--steps",
                    help="comma-separated subset: universe,bhavcopy,fundamentals,"
                         "prices,announcements")
    il.set_defaults(func=_cmd_import_legacy)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
