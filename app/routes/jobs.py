from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.db import get_db
from app.routes.auth import login_required
from app.services.mock_catalog import CATALOGS
from app.services.runner import run_job

bp = Blueprint("jobs", __name__)

LIST_TYPES = {"must": "必收", "block": "不收"}


@bp.route("/")
@login_required
def list_jobs():
    db = get_db()
    jobs = db.execute(
        "SELECT * FROM crawl_jobs ORDER BY id DESC"
    ).fetchall()
    counts = {
        row["job_id"]: row["c"]
        for row in db.execute(
            "SELECT job_id, COUNT(*) AS c FROM crawl_records GROUP BY job_id"
        )
    }
    return render_template(
        "jobs_list.html",
        jobs=jobs,
        counts=counts,
        seeds=sorted(CATALOGS.keys()),
    )


@bp.post("/jobs")
@login_required
def create_job():
    name = request.form.get("name", "").strip()
    seed = request.form.get("seed_path", "").strip()
    if not name or not seed:
        flash("请填写任务名与种子路径", "error")
        return redirect(url_for("jobs.list_jobs"))
    if seed not in CATALOGS:
        flash("种子路径必须是已注册的 mock 目录", "error")
        return redirect(url_for("jobs.list_jobs"))
    db = get_db()
    db.execute(
        "INSERT INTO crawl_jobs (name, seed_path, status) VALUES (?, ?, 'draft')",
        (name, seed),
    )
    db.commit()
    flash("任务已创建", "ok")
    return redirect(url_for("jobs.list_jobs"))


@bp.post("/jobs/<int:job_id>/run")
@login_required
def run_job_view(job_id: int):
    try:
        summary = run_job(job_id)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("jobs.detail", job_id=job_id))
    except Exception as e:
        flash(f"运行失败: {e}", "error")
        return redirect(url_for("jobs.detail", job_id=job_id))
    if summary.get("status") == "failed":
        flash(f"运行失败：{summary.get('reason', 'unknown')}", "error")
        return redirect(url_for("jobs.detail", job_id=job_id))
    parts = [
        f"写入 {summary['inserted']} 条",
        f"去重跳过 {summary['skipped_dup']} 条",
        f"被挡 {summary.get('blocked', 0)} 条",
    ]
    missing = summary.get("missing_must") or []
    if missing:
        parts.append(f"缺必收 {len(missing)} 个：{', '.join(missing)}")
    flash("运行完成：" + "，".join(parts), "ok")
    return redirect(url_for("jobs.detail", job_id=job_id))


def _get_job_or_redirect(db, job_id: int):
    job = db.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        flash("任务不存在", "error")
        return None, redirect(url_for("jobs.list_jobs"))
    return job, None


@bp.post("/jobs/<int:job_id>/lists/<list_type>")
@login_required
def add_list_entry(job_id: int, list_type: str):
    db = get_db()
    job, err = _get_job_or_redirect(db, job_id)
    if err is not None:
        return err
    if list_type not in LIST_TYPES:
        flash("清单类型不存在", "error")
        return redirect(url_for("jobs.detail", job_id=job_id))
    if job["status"] != "draft":
        flash("任务已开跑，必收/不收清单已锁定，改动被挡下", "error")
        return redirect(url_for("jobs.detail", job_id=job_id))
    code = request.form.get("code", "").strip()
    if not code:
        flash("编码不能为空，两单都加不进去", "error")
        return redirect(url_for("jobs.detail", job_id=job_id))
    other = "block" if list_type == "must" else "must"
    clash = db.execute(
        "SELECT 1 FROM job_list_entries WHERE job_id = ? AND list_type = ? AND code = ?",
        (job_id, other, code),
    ).fetchone()
    if clash is not None:
        flash(
            f"编码 {code} 已在{LIST_TYPES[other]}单里，同一编码不能同时出现在两单",
            "error",
        )
        return redirect(url_for("jobs.detail", job_id=job_id))
    db.execute(
        "INSERT OR IGNORE INTO job_list_entries (job_id, list_type, code) VALUES (?, ?, ?)",
        (job_id, list_type, code),
    )
    db.commit()
    flash(f"已加入{LIST_TYPES[list_type]}单：{code}", "ok")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.post("/jobs/<int:job_id>/lists/<list_type>/<int:entry_id>/delete")
@login_required
def delete_list_entry(job_id: int, list_type: str, entry_id: int):
    db = get_db()
    job, err = _get_job_or_redirect(db, job_id)
    if err is not None:
        return err
    if list_type not in LIST_TYPES:
        flash("清单类型不存在", "error")
        return redirect(url_for("jobs.detail", job_id=job_id))
    if job["status"] != "draft":
        flash("任务已开跑，必收/不收清单已锁定，改动被挡下", "error")
        return redirect(url_for("jobs.detail", job_id=job_id))
    db.execute(
        "DELETE FROM job_list_entries WHERE id = ? AND job_id = ? AND list_type = ?",
        (entry_id, job_id, list_type),
    )
    db.commit()
    flash(f"已从{LIST_TYPES[list_type]}单移除", "ok")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.get("/jobs/<int:job_id>")
@login_required
def detail(job_id: int):
    db = get_db()
    job = db.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        flash("任务不存在", "error")
        return redirect(url_for("jobs.list_jobs"))
    records = db.execute(
        "SELECT * FROM crawl_records WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()
    must_entries = db.execute(
        "SELECT * FROM job_list_entries WHERE job_id = ? AND list_type = 'must' ORDER BY id",
        (job_id,),
    ).fetchall()
    block_entries = db.execute(
        "SELECT * FROM job_list_entries WHERE job_id = ? AND list_type = 'block' ORDER BY id",
        (job_id,),
    ).fetchall()
    blocked = db.execute(
        "SELECT * FROM blocked_items WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()
    missing = db.execute(
        "SELECT * FROM missing_musts WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()
    return render_template(
        "job_detail.html",
        job=job,
        records=records,
        must_entries=must_entries,
        block_entries=block_entries,
        blocked=blocked,
        missing=missing,
        list_locked=job["status"] != "draft",
    )
