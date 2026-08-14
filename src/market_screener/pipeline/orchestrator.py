"""
Pipeline orchestrator.

Sequential, resumable, and skip-aware. Each stage records its start, its input
hash and its outcome in screen_stage, so a killed run can be resumed at the right
point and an unchanged re-run costs nothing.

The skip rule is the same idea as ai-trading-system's: hash what a stage reads;
if the hash matches a previous completed run of that stage for the same as_of,
there is nothing new to compute.
"""

from __future__ import annotations

import logging
import traceback
from datetime import date, datetime
from typing import Callable

from ..config import Settings
from ..db.connection import Database
from .context import (IST, RunContext, StageResult, compute_input_hash,
                      data_fingerprint, new_run_id)
from .stages import s80_phase1_screen, s85_phase1_outputs, s90_summary, s95_qc

log = logging.getLogger(__name__)

PIPELINE_ORDER: list[tuple[str, Callable[[RunContext], StageResult]]] = [
    (s80_phase1_screen.STAGE, s80_phase1_screen.run),
    (s85_phase1_outputs.STAGE, s85_phase1_outputs.run),
    (s90_summary.STAGE, s90_summary.run),
    (s95_qc.STAGE, s95_qc.run),
]

# Stages whose output is a file set rather than a database write cannot be
# skipped on an unchanged hash - the artifacts must exist on disk for this run.
NEVER_SKIP = {s85_phase1_outputs.STAGE, s90_summary.STAGE, s95_qc.STAGE}


def _open_run(db: Database, ctx: RunContext, fingerprint: dict) -> None:
    db.execute("""
        INSERT INTO market.screen_run
            (run_id, phase, status, as_of_date, universe_claim, config_hash, params)
        VALUES (%s, %s, 'running', %s, 'partial', %s, %s)
        ON CONFLICT (run_id) DO NOTHING
    """, (ctx.run_id, ctx.phase, ctx.as_of, ctx.settings.config_hash(),
          __import__("json").dumps({**ctx.params, "fingerprint": fingerprint},
                                   default=str)))


def _close_run(db: Database, ctx: RunContext, status: str, counts: dict) -> None:
    db.execute("""
        UPDATE market.screen_run
        SET    status = %s, completed_at = now(), counts = %s
        WHERE  run_id = %s
    """, (status, __import__("json").dumps(counts, default=str), ctx.run_id))


def _previous_matching_run(db: Database, stage: str, as_of: date,
                           input_hash: str, config_hash: str) -> str | None:
    """
    The most recent run whose completed `stage` read exactly these inputs under
    exactly this configuration.

    The config_hash predicate is deliberately redundant - it is already inside
    input_hash. It is here as a second, independent barrier, because the cost of
    the two disagreeing is a run that silently serves another configuration's
    candidate list while recording its own hash. A redundant WHERE clause is a
    cheap price for making that unrepresentable.
    """
    return db.fetch_value("""
        SELECT st.run_id
        FROM   market.screen_stage st
        JOIN   market.screen_run r USING (run_id)
        WHERE  st.stage = %s AND st.status IN ('complete', 'skipped')
          AND  st.input_hash = %s
          AND  r.as_of_date = %s AND r.status IN ('complete', 'partial')
          AND  r.config_hash = %s
        ORDER  BY st.finished_at DESC NULLS LAST
        LIMIT  1
    """, (stage, input_hash, as_of, config_hash))


def _record_stage(db: Database, ctx: RunContext, stage: str, status: str,
                  input_hash: str | None, res: StageResult | None = None,
                  error: str | None = None) -> None:
    db.execute("""
        INSERT INTO market.screen_stage
            (run_id, stage, attempt, status, skip_reason, input_hash,
             finished_at, rows_in, rows_out, error)
        VALUES (%s,%s,1,%s,%s,%s, now(), %s,%s,%s)
        ON CONFLICT (run_id, stage, attempt) DO UPDATE SET
            status = EXCLUDED.status, skip_reason = EXCLUDED.skip_reason,
            input_hash = EXCLUDED.input_hash, finished_at = EXCLUDED.finished_at,
            rows_in = EXCLUDED.rows_in, rows_out = EXCLUDED.rows_out,
            error = EXCLUDED.error
    """, (ctx.run_id, stage, status,
          res.skip_reason if res else None, input_hash,
          res.rows_in if res else None, res.rows_out if res else None,
          error or (res.error if res else None)))


def _record_artifacts(db: Database, ctx: RunContext, stage: str,
                      res: StageResult) -> None:
    for a in res.artifacts:
        db.execute("""
            INSERT INTO market.screen_artifact
                (run_id, stage, attempt, artifact_name, artifact_type, path,
                 row_count, sha256, bytes)
            VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (run_id, stage, attempt, artifact_name) DO UPDATE SET
                path = EXCLUDED.path, row_count = EXCLUDED.row_count,
                sha256 = EXCLUDED.sha256, bytes = EXCLUDED.bytes,
                created_at = now()
        """, (ctx.run_id, stage, a.name, a.kind, str(a.path),
              a.row_count, a.sha256, a.bytes))


def find_resumable(db: Database, as_of: date, phase: int = 1) -> str | None:
    # Scoped by phase: without it, resuming a Phase 2 run could pick up an
    # abandoned Phase 1 run for the same date and try to continue it with the
    # wrong stage list.
    return db.fetch_value("""
        SELECT run_id FROM market.screen_run
        WHERE  status = 'running' AND as_of_date = %s AND phase = %s
        ORDER  BY started_at DESC LIMIT 1
    """, (as_of, phase))


def completed_stages(db: Database, run_id: str) -> set[str]:
    rows = db.fetch_all("""
        SELECT stage FROM market.screen_stage
        WHERE  run_id = %s AND status IN ('complete', 'skipped')
    """, (run_id,))
    return {r["stage"] for r in rows}


def run_phase1(settings: Settings, db: Database, *, as_of: date | None = None,
               force: bool = False, resume: bool = False,
               stages: list[str] | None = None) -> dict:
    return _run(settings, db, phase=1, order=PIPELINE_ORDER, never_skip=NEVER_SKIP,
                as_of=as_of, force=force, resume=resume, stages=stages,
                counts=("evaluated", "eligible", "selected"))


def run_phase2(settings: Settings, db: Database, *, as_of: date | None = None,
               force: bool = False, resume: bool = False,
               stages: list[str] | None = None) -> dict:
    """
    Phase 2 runs on the same machinery as Phase 1.

    Run tracking, stage caching, resume, artifact checksums and the config-aware
    input hash are all phase-agnostic, and a second copy of them would be a
    second place for the cache to start crossing configurations.
    """
    from .stages import (s110_phase2_assess, s115_phase2_outputs,
                         s120_phase2_summary, s125_phase2_qc)

    order = [(s.STAGE, s.run) for s in (s110_phase2_assess, s115_phase2_outputs,
                                        s120_phase2_summary, s125_phase2_qc)]
    never = {s115_phase2_outputs.STAGE, s120_phase2_summary.STAGE,
             s125_phase2_qc.STAGE}
    return _run(settings, db, phase=2, order=order, never_skip=never,
                as_of=as_of, force=force, resume=resume, stages=stages,
                counts=("reviewed", "cleared", "selected"))


def _run(settings: Settings, db: Database, *, phase: int,
         order: list[tuple[str, Callable[[RunContext], StageResult]]],
         never_skip: set[str], counts: tuple[str, ...],
         as_of: date | None = None, force: bool = False, resume: bool = False,
         stages: list[str] | None = None) -> dict:
    as_of = as_of or date.today()

    run_id = find_resumable(db, as_of, phase) if resume else None
    done: set[str] = set()
    if run_id:
        done = completed_stages(db, run_id)
        log.info("resuming run %s; %d stage(s) already complete", run_id, len(done))
    else:
        run_id = new_run_id(phase, as_of)

    ctx = RunContext(run_id=run_id, phase=phase, as_of=as_of, settings=settings,
                     db=db, params={"force": force, "stages": stages})
    ctx.state["started_at"] = datetime.now(IST).isoformat()

    fingerprint = data_fingerprint(db, as_of, settings)
    _open_run(db, ctx, fingerprint)
    log.info("phase %d run %s (as_of=%s, basis=%s)", phase, run_id, as_of,
             settings.price_basis)

    summary: dict[str, dict] = {}
    status = "complete"

    for stage, fn in order:
        if stages and stage not in stages:
            continue
        if stage in done:
            log.info("%s already complete in this run - skipping", stage)
            summary[stage] = {"status": "skipped", "reason": "already_complete"}
            continue

        input_hash = compute_input_hash({"stage": stage, **fingerprint})
        if not force and stage not in never_skip:
            prior = _previous_matching_run(db, stage, as_of, input_hash,
                                           settings.config_hash())
            if prior and prior != ctx.run_id:
                log.info("%s inputs unchanged - reusing %s", stage, prior)
                # A skipped stage must still leave its output available to THIS
                # run: downstream stages read by run_id, and every run has to be
                # self-contained for `runs diff` to mean anything.
                reuse_fn = getattr(fn.__module__ and __import__(
                    fn.__module__, fromlist=["reuse"]), "reuse", None)
                res = (reuse_fn(ctx, prior) if callable(reuse_fn)
                       else StageResult(stage=stage, status="skipped",
                                        skip_reason="unchanged_stage_inputs"))
                _record_stage(db, ctx, stage, "skipped", input_hash, res)
                summary[stage] = {"status": "skipped", **res.detail}
                continue

        db.execute("""
            INSERT INTO market.screen_stage (run_id, stage, attempt, status, input_hash)
            VALUES (%s,%s,1,'running',%s)
            ON CONFLICT (run_id, stage, attempt) DO UPDATE SET
                status = 'running', started_at = now(), input_hash = EXCLUDED.input_hash
        """, (ctx.run_id, stage, input_hash))

        try:
            res = fn(ctx)
        except Exception as exc:                       # noqa: BLE001
            log.exception("%s failed", stage)
            _record_stage(db, ctx, stage, "failed", input_hash,
                          error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[:2000]}")
            _close_run(db, ctx, "failed", {"failed_stage": stage})
            summary[stage] = {"status": "failed", "error": str(exc)}
            return {"run_id": run_id, "status": "failed", "stages": summary}

        _record_stage(db, ctx, stage, res.status, input_hash, res)
        _record_artifacts(db, ctx, stage, res)
        summary[stage] = {"status": res.status, **res.detail}
        if res.status == "failed":
            status = "partial"
        log.info("%s -> %s %s", stage, res.status, res.detail)

    tally = {k: ctx.state.get(k) for k in counts}
    _close_run(db, ctx, status, tally)

    return {"run_id": run_id, "status": status, "as_of": str(as_of),
            "output_dir": str(ctx.output_dir()), "counts": tally, "stages": summary}
