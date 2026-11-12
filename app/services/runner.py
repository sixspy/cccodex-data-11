import json
import sqlite3

from flask import current_app

from app.db import db_transaction, get_db
from app.services.dedup import normalize_item_key
from app.services.mock_catalog import CATALOGS, parse_catalog_html, render_catalog_page


def _list_codes(db, job_id: int, list_type: str) -> set[str]:
    rows = db.execute(
        "SELECT code FROM job_list_entries WHERE job_id = ? AND list_type = ?",
        (job_id, list_type),
    ).fetchall()
    return {row["code"].strip() for row in rows if row["code"].strip()}


def run_job(job_id: int) -> dict:
    db = get_db()
    job = db.execute(
        "SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if job is None:
        raise ValueError("job not found")
    if job["status"] == "running":
        raise ValueError("job already running")
    if job["status"] == "done":
        raise ValueError("job already done")

    must_codes = _list_codes(db, job_id, "must")
    block_codes = _list_codes(db, job_id, "block")

    seed = job["seed_path"]
    if seed not in CATALOGS:
        db.execute(
            "UPDATE crawl_jobs SET status = 'failed', finished_at = datetime('now') WHERE id = ?",
            (job_id,),
        )
        db.commit()
        return {"status": "failed", "reason": "unknown seed", "inserted": 0, "skipped_dup": 0}

    html = render_catalog_page(seed)
    if isinstance(html, tuple):
        html = html[0]
    items = parse_catalog_html(html, seed)

    seen_codes = {
        str(it.get("sku", "")).strip() for it in items if str(it.get("sku", "")).strip()
    }
    missing_codes = sorted(must_codes - seen_codes)
    extra_block_codes = sorted(block_codes - seen_codes)

    inserted = 0
    skipped_dup = 0
    blocked_in_catalog = 0
    with db_transaction() as tx:
        tx.execute(
            "UPDATE crawl_jobs SET status = 'running', finished_at = NULL WHERE id = ?",
            (job_id,),
        )
        tx.execute("DELETE FROM blocked_items WHERE job_id = ?", (job_id,))
        tx.execute("DELETE FROM missing_musts WHERE job_id = ?", (job_id,))
        for it in items:
            sku = str(it.get("sku", "")).strip()
            if sku and sku in block_codes:
                tx.execute(
                    "INSERT OR IGNORE INTO blocked_items (job_id, code, reason) VALUES (?, ?, ?)",
                    (job_id, sku, "不收清单拦截"),
                )
                blocked_in_catalog += 1
                continue
            key = normalize_item_key(seed, it["title"])
            try:
                tx.execute(
                    """
                    INSERT INTO crawl_records (job_id, item_key, title, payload_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (job_id, key, it["title"], json.dumps(it, ensure_ascii=False)),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                skipped_dup += 1
        # 右单上本次目录没出现的编码同样进被挡清单，账册里同样找不到
        for code in extra_block_codes:
            tx.execute(
                "INSERT OR IGNORE INTO blocked_items (job_id, code, reason) VALUES (?, ?, ?)",
                (job_id, code, "不收清单拦截"),
            )
        for code in missing_codes:
            tx.execute(
                "INSERT OR IGNORE INTO missing_musts (job_id, code) VALUES (?, ?)",
                (job_id, code),
            )
        tx.execute(
            "UPDATE crawl_jobs SET status = 'done', finished_at = datetime('now') WHERE id = ?",
            (job_id,),
        )

    blocked_total = blocked_in_catalog + len(extra_block_codes)
    current_app.logger.info(
        "job %s finished inserted=%s skipped_dup=%s blocked=%s missing_must=%s",
        job_id, inserted, skipped_dup, blocked_total, len(missing_codes),
    )
    return {
        "status": "done",
        "inserted": inserted,
        "skipped_dup": skipped_dup,
        "total_source": len(items),
        "blocked": blocked_total,
        "missing_must": missing_codes,
    }
