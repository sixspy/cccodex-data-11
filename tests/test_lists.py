"""必收/不收两张清单的验收测试，按需求拆成独立段落，每段只盯一件事。"""

from app.db import get_db
from app.services import runner as runner_module
from app.services.runner import run_job


def login(client):
    return client.post(
        "/login",
        data={"username": "admin", "password": "123456"},
        follow_redirects=True,
    )


def create_job(client, seed="/mock/catalog/alpha"):
    client.post(
        "/jobs",
        data={"name": "清单演练", "seed_path": seed},
        follow_redirects=True,
    )
    # client 的请求结束后连接关闭，另起一次请求拿 id
    r = client.get("/")
    text = r.get_data(as_text=True)
    import re

    m = re.search(r"/jobs/(\d+)", text)
    return int(m.group(1))


def add_entry(client, job_id, list_type, code):
    return client.post(
        f"/jobs/{job_id}/lists/{list_type}",
        data={"code": code},
        follow_redirects=True,
    )


def run(client, job_id):
    return client.post(
        f"/jobs/{job_id}/run", follow_redirects=True
    )


def list_codes(app, job_id, list_type):
    with app.app_context():
        rows = get_db().execute(
            "SELECT code FROM job_list_entries WHERE job_id = ? AND list_type = ?",
            (job_id, list_type),
        ).fetchall()
    return [r["code"] for r in rows]


def test_1_same_code_cannot_enter_both_lists(app, client):
    """第一段：同一编码进了左单（必收）就进不了右单（不收），反向亦然。"""
    login(client)
    job_id = create_job(client)

    r = add_entry(client, job_id, "must", "A-001")
    assert "已加入必收" in r.get_data(as_text=True)

    r = add_entry(client, job_id, "block", "A-001")
    assert r.status_code == 200
    assert "同一编码不能同时出现在两单" in r.get_data(as_text=True)
    assert list_codes(app, job_id, "block") == []

    r = add_entry(client, job_id, "block", "B-101")
    assert "已加入不收" in r.get_data(as_text=True)
    r = add_entry(client, job_id, "must", "B-101")
    assert "同一编码不能同时出现在两单" in r.get_data(as_text=True)
    assert list_codes(app, job_id, "must") == ["A-001"]


def test_2_blank_code_rejected_by_both_lists(app, client):
    """第二段：什么都不填（空编码），两单都加不进去。"""
    login(client)
    job_id = create_job(client)

    for list_type in ("must", "block"):
        r = add_entry(client, job_id, list_type, "   ")
        assert r.status_code == 200
        assert "编码不能为空" in r.get_data(as_text=True)
        assert list_codes(app, job_id, list_type) == []


def test_3_block_codes_only_live_in_blocked_list(app, client):
    """第三段：右单编码开跑后只活在被挡清单里，账册（落库记录）找不到它们。"""
    login(client)
    job_id = create_job(client)
    add_entry(client, job_id, "block", "A-002")

    with app.app_context():
        summary = run_job(job_id)
    assert summary["status"] == "done"

    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    # 被挡清单里有该编码
    assert "A-002" in page
    with app.app_context():
        blocked = get_db().execute(
            "SELECT code FROM blocked_items WHERE job_id = ?", (job_id,)
        ).fetchall()
        records = get_db().execute(
            "SELECT payload_json FROM crawl_records WHERE job_id = ?", (job_id,)
        ).fetchall()
    assert [r["code"] for r in blocked] == ["A-002"]
    # 账册里任何一条记录的 payload 都不含 A-002
    assert all("A-002" not in r["payload_json"] for r in records)
    # 被挡清单在页面上的落库记录之后（页面下方），用 h2 标题定位避免命中说明文字
    assert page.index("<h2>落库记录（账册）</h2>") < page.index("<h2>被挡清单</h2>")


def test_4_missing_must_codes_all_listed_on_card(app, client):
    """第四段：缺左单编码时，卡片打出缺必收，并把缺掉的编码列全。"""
    login(client)
    job_id = create_job(client)
    for code in ("A-001", "A-999", "A-888"):
        add_entry(client, job_id, "must", code)

    r = run(client, job_id)
    page = r.get_data(as_text=True)
    assert "<h2>缺必收</h2>" in page

    import re

    section = re.search(
        r'<section class="card missing-card">(.*?)</section>', page, re.S
    ).group(1)
    assert "A-999" in section
    assert "A-888" in section
    # A-001 这次目录里有，不算缺，不得出现在缺必收卡片里
    assert "A-001" not in section

    with app.app_context():
        missing = get_db().execute(
            "SELECT code FROM missing_musts WHERE job_id = ? ORDER BY code",
            (job_id,),
        ).fetchall()
    assert [r["code"] for r in missing] == ["A-888", "A-999"]


def test_5_all_must_seen_no_missing_card(app, client):
    """第五段：左单编码这次都能见到时，卡片不打出缺必收。"""
    login(client)
    job_id = create_job(client)
    for code in ("A-001", "A-002", "A-003"):
        add_entry(client, job_id, "must", code)

    page = run(client, job_id).get_data(as_text=True)
    # 说明文字里会提到“缺必收”，这里精确判断缺必收卡片没有渲染
    assert 'class="card missing-card"' not in page
    assert "<h2>缺必收</h2>" not in page
    with app.app_context():
        assert get_db().execute(
            "SELECT COUNT(*) AS c FROM missing_musts WHERE job_id = ?", (job_id,)
        ).fetchone()["c"] == 0


def test_6_editing_lists_while_running_is_rejected(app, client, monkeypatch):
    """第六段：开跑途中（status=running）改单会被挡下，清单内容不变。"""
    login(client)
    job_id = create_job(client)
    add_entry(client, job_id, "must", "A-001")

    real_run = runner_module.run_job

    def stuck_running(jid):
        # 模拟跑到一半：状态停在 running
        with app.app_context():
            db = get_db()
            db.execute(
                "UPDATE crawl_jobs SET status = 'running' WHERE id = ?", (jid,)
            )
            db.commit()
        raise ValueError("job already running")

    # 路由模块里引用的是 run_job 名字，需要在那里打补丁
    from app.routes import jobs as jobs_route

    monkeypatch.setattr(jobs_route, "run_job", stuck_running)
    run(client, job_id)

    with app.app_context():
        status = get_db().execute(
            "SELECT status FROM crawl_jobs WHERE id = ?", (job_id,)
        ).fetchone()["status"]
    assert status == "running"

    for list_type, code in (("must", "A-002"), ("block", "A-003")):
        r = add_entry(client, job_id, list_type, code)
        assert "已锁定" in r.get_data(as_text=True)
    r = client.post(
        f"/jobs/{job_id}/lists/must/1/delete", follow_redirects=True
    )
    assert "已锁定" in r.get_data(as_text=True)
    assert list_codes(app, job_id, "must") == ["A-001"]
    assert list_codes(app, job_id, "block") == []

    monkeypatch.undo()
    # 收尾：让任务正常结束，避免污染同库后续断言（每个用例独立库，仅保险）
    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE crawl_jobs SET status = 'draft' WHERE id = ?", (job_id,)
        )
        db.commit()
        real_run(job_id)


def test_7_list_titles_tell_them_apart(app, client):
    """第七段：两张单子的标题让人分开哪张该收——左单必收，右单不收。"""
    login(client)
    job_id = create_job(client)
    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)

    must_pos = page.index(">必收<")
    block_pos = page.index(">不收<")
    assert must_pos < block_pos  # 左单在前、右单在后
    assert "被挡清单" in page
