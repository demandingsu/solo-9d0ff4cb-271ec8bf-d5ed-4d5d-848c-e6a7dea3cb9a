"""规模/并发压力试跑（标准库即可运行）。

用法（在项目根目录）：
    APP_DB_PATH=/tmp/stress-maintenance.db \
    PYTHONPATH=/tmp/pylibs:. python3 -I tests/run_stress.py

覆盖任务点名的三类场景：
  C. 集中归科：5000 工时 / 10100 分派 / 100 归科，验证供数查询与纠错的
     核算规模由源表行数和关联键数决定（集合式 SQL，不逐条展开路径组合），
     扣留行路径数准确，且供数只对恰好一条路径的工时返回科目；
  D. 撤除共享归科：集中归科记录被大量工时共用，撤除必须被 409 拦截，
     原数据与审计保持不变；
  E. 并发录入：纠错写事务持锁期间，其他月份并发录入靠 busy_timeout 串行通过，
     不出现超时/500，最终数据自洽。
另含：同科目重复仍判重复、恢复行号回执、读不阻塞写。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8001")
MONTH = "2026-11"
PASS = "[PASS]"
FAIL = "[FAIL]"
failures: list[str] = []

N_CONC = 100          # 集中归科的重复工时数（每个 2 条分派、同项目同科目）
N_SINGLE = 4400       # 恰好一条路径的工时
N_NOPATH = 500        # 无任何分派的工时
N_TOTAL = N_CONC + N_SINGLE + N_NOPATH


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = PASS if cond else FAIL
    print(f"  {mark} {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def call(method: str, path: str, body: dict | None = None, timeout: float = 40.0) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def wait_ready(proc: subprocess.Popen) -> None:
    for _ in range(80):
        if proc.poll() is not None:
            raise RuntimeError("服务进程提前退出")
        try:
            status, _ = call("GET", "/health")
            if status == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("服务未在 40s 内就绪")


def wait_ready_url() -> None:
    for _ in range(80):
        try:
            status, _ = call("GET", "/health")
            if status == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("外部服务未就绪")


def build_dataset() -> None:
    call("DELETE", f"/months/{MONTH}")

    wh_rows = [
        {"row_id": i, "work_order": f"WO{i:05d}", "minutes": 30 + i % 60}
        for i in range(1, N_TOTAL + 1)
    ]
    t0 = time.perf_counter()
    status, r = call("POST", f"/months/{MONTH}/work-hours", {"rows": wh_rows})
    check("录入工时 5000 行", status == 200 and r.get("inserted") == N_TOTAL, str(r))

    # 分派：WO1..WO100 各 2 条分派且都指向 P0（同项目同科目 -> 路径数=2，同科目也算重复）；
    # WO101..WO4500 各 1 条分派到专属项目 P(i)；WO4501..WO5000 无分派（path_count=0）。
    dispatches: list[dict] = []
    rid = 0
    for i in range(1, N_CONC + 1):
        rid += 1
        dispatches.append({"row_id": rid, "work_order": f"WO{i:05d}", "project": "P0"})
        rid += 1
        dispatches.append({"row_id": rid, "work_order": f"WO{i:05d}", "project": "P0"})
    for i in range(N_CONC + 1, N_CONC + N_SINGLE + 1):
        rid += 1
        dispatches.append({"row_id": rid, "work_order": f"WO{i:05d}", "project": f"P{i}"})
    status, r = call("POST", f"/months/{MONTH}/dispatches", {"rows": dispatches})
    check(f"录入分派 {len(dispatches)} 行", status == 200 and r.get("inserted") == len(dispatches), str(r))

    # 归科：P0 一条（100 个重复工时 + 200 条分派集中到这一条），其余每个专属项目各一条。
    attributions = [{"row_id": 1, "project": "P0", "budget_subject": "SUBJ-CONC"}]
    for i in range(N_CONC + 1, N_CONC + N_SINGLE + 1):
        attributions.append(
            {"row_id": i - N_CONC + 1, "project": f"P{i}", "budget_subject": f"SUBJ-{i}"}
        )
    status, r = call("POST", f"/months/{MONTH}/attributions", {"rows": attributions})
    check(f"录入归科 {len(attributions)} 行", status == 200 and r.get("inserted") == len(attributions), str(r))
    print(f"    数据集装载耗时 {time.perf_counter() - t0:.2f}s")


def scenario_concentrated() -> dict:
    print("场景 C：集中归科 + 集合式供数核算（5000 工时 / 万级分派）")
    build_dataset()

    t0 = time.perf_counter()
    status, r = call("GET", f"/months/{MONTH}/supply")
    elapsed = time.perf_counter() - t0
    check("供数查询返回 200", status == 200, str(r))
    print(f"    全月供数查询耗时 {elapsed * 1000:.0f} ms")
    check("供数查询在 2s 内完成（规模由行数/键数决定）", elapsed < 2.0, f"{elapsed:.3f}s")

    sup = {x["work_row_id"]: x for x in r["suppliable"]}
    wh = {x["work_row_id"]: x for x in r["withheld"]}

    check("可供数恰好 4400 行", len(sup) == N_SINGLE, str(len(sup)))
    check("扣留恰好 600 行", len(wh) == N_CONC + N_NOPATH, str(len(wh)))
    check("集中工时 WO1 扣留且路径数=2（同科目也算重复）",
          wh.get(1, {}).get("path_count") == 2 and wh[1]["reason"] == "duplicate", str(wh.get(1)))
    check("集中工时 WO100 路径数=2", wh.get(100, {}).get("path_count") == 2, str(wh.get(100)))
    check("无路径工时 WO5000 路径数=0 原因 no_path",
          wh.get(5000, {}).get("path_count") == 0 and wh[5000]["reason"] == "no_path", str(wh.get(5000)))
    check("扣留行不返回预算科目", all("budget_subject" not in x for x in r["withheld"]), "")
    sample = sup.get(2000)
    check("单路径工时 WO2000 供数且带唯一科目",
          sample is not None and sample["budget_subject"] == "SUBJ-2000" and sample["path_count"] == 1,
          str(sample))

    # 正确纠错：撤除 WO1 的第二条分派（row_id=2），只恢复 WO1；其他 99 个重复工时不受影响。
    t0 = time.perf_counter()
    status, r = call("POST", f"/months/{MONTH}/corrections",
                     {"work_row_id": 1, "remove": "dispatch", "remove_row_id": 2})
    corr_elapsed = time.perf_counter() - t0
    check("集中数据下纠错成功（200）", status == 200, f"{status} {r}")
    print(f"    万级行纠错事务耗时 {corr_elapsed * 1000:.0f} ms（含两次全月集合核算）")
    check("纠错在 2s 内完成（写锁持有时间短）", corr_elapsed < 2.0, f"{corr_elapsed:.3f}s")
    check("恢复行号回执恰好 [1]", r.get("restored_work_row_ids") == [1], str(r.get("restored_work_row_ids")))
    sup = {x["work_row_id"]: x for x in r["suppliable"]}
    wh = {x["work_row_id"]: x for x in r["withheld"]}
    check("WO1 恢复供数且科目 SUBJ-CONC", sup.get(1, {}).get("budget_subject") == "SUBJ-CONC", str(sup.get(1)))
    check("WO2..WO100 仍扣留且路径数仍为 2",
          all(wh.get(i, {}).get("path_count") == 2 for i in range(2, 101)), "")
    check("单路径工时全部保科", len(sup) == N_SINGLE + 1, str(len(sup)))
    return {"supply_ms": elapsed * 1000, "correct_ms": corr_elapsed * 1000}


def scenario_shared_attribution() -> None:
    print("场景 D：撤除共享归科（归科 row_id=1 被 100 个工时共用）")
    status, r = call("GET", f"/months/{MONTH}/audit")
    audit_before = len(r["audits"])

    # WO2 仍为重复（2 条路径都走 P0 -> 归科 1）。撤除归科 1 会同时打掉单路径工时
    # WO1（刚恢复）与 WO101..WO4500 的科目，必须被保科规则拦截。
    status, r = call("POST", f"/months/{MONTH}/corrections",
                     {"work_row_id": 2, "remove": "attribution", "remove_row_id": 1})
    check("撤除共享归科被拒（409）", status == 409, f"{status} {r}")
    check("回执给出中文原因", bool(r.get("detail")), str(r))

    status, r = call("GET", f"/months/{MONTH}/audit")
    check("失败不写审计（审计条数不变）", len(r["audits"]) == audit_before,
          f"{len(r['audits'])} vs {audit_before}")

    status, r = call("GET", f"/months/{MONTH}/supply")
    sup = {x["work_row_id"]: x for x in r["suppliable"]}
    wh = {x["work_row_id"]: x for x in r["withheld"]}
    check("回滚后 WO1 仍供数、科目不变", sup.get(1, {}).get("budget_subject") == "SUBJ-CONC", str(sup.get(1)))
    check("回滚后 WO101 等单路径工时全部保科",
          all(i in sup for i in (101, 2000, 4500)), "")
    check("回滚后 WO2 仍为重复扣留", wh.get(2, {}).get("path_count") == 2, str(wh.get(2)))

    # 不存在的记录 -> 404；不在路径上的记录 -> 422，均不得改动数据。
    status, _ = call("POST", f"/months/{MONTH}/corrections",
                     {"work_row_id": 2, "remove": "attribution", "remove_row_id": 999999})
    check("撤除不存在的归科 404", status == 404, str(status))
    # 分派行 5 属于 WO3，不在 WO2 的路径上 -> 422，均不得改动数据。
    status, _ = call("POST", f"/months/{MONTH}/corrections",
                     {"work_row_id": 2, "remove": "dispatch", "remove_row_id": 5})
    check("撤除不在路径上的分派 422", status == 422, str(status))


def scenario_concurrent_entry() -> None:
    print("场景 E：纠错持锁期间其他月份并发录入")
    other_months = ["2026-12", "2027-01", "2027-02", "2027-03"]
    errors: list[str] = []
    durations: list[float] = []

    def writer(month: str) -> None:
        barrier.wait(timeout=30)  # 与主线程到齐后同时发出，制造锁竞争窗口
        body = {
            "rows": [
                {"row_id": 1, "work_order": f"{month}-WO1", "minutes": 11},
                {"row_id": 2, "work_order": f"{month}-WO2", "minutes": 22},
            ]
        }
        t0 = time.perf_counter()
        status, r = call("POST", f"/months/{month}/work-hours", body, timeout=60)
        durations.append(time.perf_counter() - t0)
        if status != 200:
            errors.append(f"{month} -> {status} {r}")
        # 同一月再录分派/归科，验证多张表写入都能串行通过
        s2, r2 = call("POST", f"/months/{month}/dispatches",
                      {"rows": [{"row_id": 1, "work_order": f"{month}-WO1", "project": "PX"}]})
        s3, r3 = call("POST", f"/months/{month}/attributions",
                      {"rows": [{"row_id": 1, "project": "PX", "budget_subject": "SX"}]})
        if s2 != 200 or s3 != 200:
            errors.append(f"{month} 分派/归科 -> {s2} {r2} / {s3} {r3}")

    # 主线程连续发起纠错（持写锁），工作线程同时向其他月份录入。
    barrier = threading.Barrier(len(other_months) + 1)
    threads = [threading.Thread(target=writer, args=(m,)) for m in other_months]
    for t in threads:
        t.start()
    barrier.wait(timeout=30)  # 5 方到齐后同时放行，避免单方永久等待
    # 用撤除 WO3..WO6 的一条分派制造连续写锁窗口（它们当前仍是 path_count=2）
    corr_codes: list[int] = []
    for wid in range(3, 7):
        remove_rid = wid * 2  # build_dataset 中 WOi 的第二条分派 row_id
        status, _ = call("POST", f"/months/{MONTH}/corrections",
                         {"work_row_id": wid, "remove": "dispatch", "remove_row_id": remove_rid})
        corr_codes.append(status)
    for t in threads:
        t.join()

    check("并发期间 4 次纠错全部 200", corr_codes == [200] * 4, str(corr_codes))
    check("其他月份并发录入无超时/500/409（busy_timeout 内串行通过）", not errors, "; ".join(errors))
    print(f"    并发录入请求耗时区间 {min(durations) * 1000:.0f}–{max(durations) * 1000:.0f} ms")

    for month in other_months:
        status, r = call("GET", f"/months/{month}/supply")
        sup = {x["work_row_id"]: x for x in r["suppliable"]}
        check(f"{month} 数据自洽：WO1 供数带科目 SX、WO2 无路径扣留",
              status == 200 and sup.get(1, {}).get("budget_subject") == "SX"
              and [x["work_row_id"] for x in r["withheld"]] == [2], str(r))

    # 读不阻塞写：纠错事务外，供数查询在并发写入后仍即时可用
    status, r = call("GET", f"/months/{MONTH}/supply")
    check("主月份供数视图最终自洽（已恢复 WO1/WO3..WO6）", status == 200
          and sorted(x["work_row_id"] for x in r["suppliable"])[:6] == [1, 3, 4, 5, 6, 101], str(r)[:200])
    for month in other_months:
        call("DELETE", f"/months/{month}")


def main() -> int:
    external = os.environ.get("SERVER_EXTERNAL") == "1"
    proc = None
    if external:
        wait_ready_url()
    else:
        log = open("/tmp/stress-uvicorn.log", "w")
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # 透传当前解释器可见的全部 site 路径，兼容隔离模式（-I）与精简环境。
        child_paths = [project_root, *sys.path, os.environ.get("PYTHONPATH", "")]
        seen: set[str] = set()
        path_parts = [p for p in child_paths
                      if p and p not in seen and not seen.add(p)]
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8001"],
            cwd=project_root,
            env={**os.environ, "PYTHONPATH": os.pathsep.join(path_parts)},
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        wait_ready(proc)
    try:
        metrics = scenario_concentrated()
        scenario_shared_attribution()
        scenario_concurrent_entry()
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if failures:
            try:
                with open("/tmp/stress-uvicorn.log") as fh:
                    print("\n".join(fh.read().splitlines()[-20:]))
            except OSError:
                pass
    print()
    if failures:
        print(f"{FAIL} 共 {len(failures)} 项失败: {failures}")
        return 1
    print(f"{PASS} 压力试跑全部断言通过"
          f"（供数查询 {metrics['supply_ms']:.0f}ms / 纠错事务 {metrics['correct_ms']:.0f}ms @ 5k 工时）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
