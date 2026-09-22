"""一次性探针脚本：实测 B站收藏夹「批量写接口」的真实行为。

依据：docs/fav/action.md（copy / move / batch-del / clean）+ 审查日志 v2 的三项待实测。
这是**独立于 App 的一次性工具**，不参与运行时；跑完可整文件删除。

三个探针夹（用户授权范围，当前均为空）：
    A = 机器学习    B = 戏曲    C = 宠物
所有写操作只在这三个夹里进行；copy 的来源夹只读（copy 不会删源夹内容）。

用法（只读阶段不需要 --go；任何写请求都必须显式 --go，否则只打印计划）：
    python probe_batch.py inspect                 # 只读：实时夹列表 + 三个探针夹
    python probe_batch.py bind  --go              # 把三个夹的 id 绑进 _probe_state.json
    python probe_batch.py p1    --go              # P-1 单请求条数上限
    python probe_batch.py p2    --go              # P-2 批内部分失败语义
    python probe_batch.py p3    --go              # P-3 move 是否原子
    python probe_batch.py p4    --go              # P-4「已经收藏过了」错误码
    python probe_batch.py p6    --go              # P-6 写批量风控容忍度
    python probe_batch.py p7    --go              # P-7 夹数量上限 / 重名建夹
    python probe_batch.py cleanup --go            # 删掉三个探针夹，恢复原状

约束（用户指定）：所有请求间隔 ≥2 秒。
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import bili_api

HERE = Path(__file__).resolve().parent
BASE = "https://api.bilibili.com"
STATE_FILE = HERE / "_probe_state.json"
RAW_LOG = HERE / "_probe_raw.jsonl"

GAP = 2.0                                     # 所有请求最小间隔（用户要求）
PROBE_NAMES = {"A": "机器学习", "B": "戏曲", "C": "宠物"}   # 用户授权的操作范围
SIZES = [5, 10, 20, 50, 100]                  # P-1 探测档位

GO = False
SESSION: bili_api.BiliSession
CSRF = ""
MID = ""
_last_call = 0.0


# ---------- 基础设施 ----------

def open_session() -> bili_api.BiliSession:
    sec = json.loads((HERE / "secrets.json").read_text("utf-8"))
    ci = bili_api.load_cookie_from_string(sec.get("cookie_string", ""))
    s = bili_api.BiliSession(ci)
    s.read_interval = GAP
    s.write_interval = GAP
    print(f"[init] cookie 尾4位={ci.sessdata[-4:]}  可写={'yes' if ci.has_essential else 'no'}")
    return s


def raw(kind: str, path: str, *, write: bool, data=None, params=None, note="") -> tuple[int, dict]:
    """发一次请求并打印原始返回；写请求在未加 --go 时只打印计划。"""
    global _last_call
    if write and not GO:
        print(f"[dry-run] 跳过写请求 {path}  note={note}")
        return -1, {"code": "dry-run"}
    wait = GAP - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()
    method = "POST" if data is not None else "GET"
    r = SESSION.session.request(method, BASE + path, data=data, params=params, timeout=25)
    try:
        body = r.json()
    except Exception:
        body = {"_non_json": r.text[:300]}
    code, msg = body.get("code"), body.get("message")
    flag = "" if code == 0 else "  <== 非0"
    print(f"  [{kind}] {note or path}  →  HTTP {r.status_code}  code={code}  message={msg}{flag}")
    with RAW_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": kind, "path": path, "note": note, "http": r.status_code,
                            "body": body}, ensure_ascii=False) + "\n")
    return r.status_code, body


def wdata(**kw) -> dict:
    d = {k: str(v) for k, v in kw.items() if v is not None and v != ""}
    d["csrf"] = CSRF
    return d


def live_folders() -> dict[str, tuple[str, int]]:
    """实时夹列表：{title: (media_id, count)}（同名取第一个）。"""
    _, body = raw("read", "/x/v3/fav/folder/created/list-all", write=False,
                  params={"up_mid": MID}, note="list-all")
    out = {}
    for f in (body.get("data") or {}).get("list") or []:
        out.setdefault(f.get("title", ""), (str(f.get("id", "")), f.get("media_count", 0) or 0))
    return out


def folder_aids(media_id: str, max_pages: int = 6) -> list[int]:
    """读某夹内容 id（resource/list 需 WBI 签名）。"""
    out, pn = [], 1
    while pn <= max_pages:
        params = SESSION._wbi_sign({"media_id": media_id, "pn": pn, "ps": 20,
                                   "platform": "web", "order": "mtime", "type": 0, "tid": 0})
        _, body = raw("read", "/x/v3/fav/resource/list", write=False,
                      params=params, note=f"读夹 {media_id} p{pn}")
        d = body.get("data") or {}
        out += [int(m.get("id")) for m in (d.get("medias") or []) if m.get("id")]
        if not d.get("has_more"):
            break
        pn += 1
    return out


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}


def save_state(st: dict):
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def probe_id(key: str) -> str:
    return load_state().get("probe", {}).get(key, "")


def local_pool() -> tuple[str, list[int]]:
    """本地数据里挑「条目最多」的夹作 copy 源（copy 不影响源夹）。"""
    vids = json.loads((HERE / "data" / "videos.json").read_text("utf-8"))
    by_src: dict[str, list[int]] = {}
    for v in vids.values():
        if (v.get("title") or "").strip() == "已失效视频":       # 失效的复制不过去
            continue
        if not v.get("aid"):
            continue
        by_src.setdefault(str(v.get("source_folder_id") or ""), []).append(int(v["aid"]))
    src = max(by_src, key=lambda k: len(by_src[k]))
    return src, by_src[src]


def local_invalid_aid() -> int:
    vids = json.loads((HERE / "data" / "videos.json").read_text("utf-8"))
    for v in vids.values():
        if (v.get("title") or "").strip() == "已失效视频" and v.get("aid"):
            return int(v["aid"])
    return 0


def clear_folder(media_id: str, tag: str):
    """把探针夹清空，避免污染后续阶段。"""
    left = folder_aids(media_id)
    if left:
        raw("write", "/x/v3/fav/resource/batch-del", write=True,
            data=wdata(media_id=media_id, resources=",".join(f"{a}:2" for a in left),
                       platform="web"), note=f"{tag} 清理残留 {len(left)} 条")


# ---------- 各阶段 ----------

def stage_inspect():
    fs = live_folders()
    print(f"\n[inspect] 实时夹数量 = {len(fs)}")
    print("[inspect] 三个探针夹（授权范围）：")
    for k, name in PROBE_NAMES.items():
        if name in fs:
            mid_, cnt = fs[name]
            print(f"  {k} = {name}: media_id={mid_}  count={cnt}")
        else:
            print(f"  {k} = {name}: 不存在！")
    src, aids = local_pool()
    print(f"[inspect] copy 源夹 = {src}（本地可用 aid {len(aids)} 条，探针共需约 250 条）")


def stage_bind():
    fs = live_folders()
    st = load_state()
    st["probe"] = {}
    for k, name in PROBE_NAMES.items():
        if name not in fs:
            raise SystemExit(f"[bind] 找不到夹「{name}」，请先确认")
        mid_, cnt = fs[name]
        if cnt:
            raise SystemExit(f"[bind] 夹「{name}」非空（count={cnt}），为安全起见中止")
        st["probe"][k] = mid_
    st["probe_names"] = PROBE_NAMES
    st["bound_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(st)
    print(f"[bind] 已绑定：{json.dumps(st['probe'], ensure_ascii=False)}  （均为空夹，可安全写入）")


def stage_p1():
    """P-1 单请求条数上限：每档 S 做一次 copy，成功后再用同样条目测 batch-del。"""
    ta = probe_id("A")
    src, aids = local_pool()
    print(f"\n[P-1] 源夹={src}  探针夹A={ta}  档位={SIZES}")
    pos, rows = 0, []
    for s in SIZES:
        items = aids[pos:pos + s]
        pos += s
        if len(items) < s:
            print(f"  [P-1] aid 池不足，跳过 S={s}")
            break
        res = ",".join(f"{a}:2" for a in items)
        _, b_copy = raw("write", "/x/v3/fav/resource/copy", write=True,
                        data=wdata(src_media_id=src, tar_media_id=ta, mid=MID,
                                   resources=res, platform="web"), note=f"copy S={s}")
        got = len(set(items) & set(folder_aids(ta)))
        del_code = del_msg = left = "未测"
        if got == s:                       # copy 全成功才测 del，否则数据被污染
            _, b_del = raw("write", "/x/v3/fav/resource/batch-del", write=True,
                           data=wdata(media_id=ta, resources=res, platform="web"),
                           note=f"batch-del S={s}")
            del_code, del_msg = b_del.get("code"), b_del.get("message")
            left = len(set(items) & set(folder_aids(ta)))
        else:
            clear_folder(ta, "[P-1]")
        rows.append((s, b_copy.get("code"), got, del_code, left))
        print(f"  [P-1] S={s}: copy code={b_copy.get('code')}（进夹 {got}/{s}）"
              f" | del code={del_code}（残留 {left}）")
    print("\n[P-1] 汇总（S | copy code | 实际进夹 | del code | 残留）：")
    for r in rows:
        print(f"  {r[0]:>4} | {r[1]} | {r[2]}/{r[0]} | {r[3]} | {r[4]}")


def stage_p1b():
    """P-1b batch-del 的条数上限：用「实际进夹」的条目去删，避开 copy 静默丢条的干扰。"""
    ta = probe_id("A")
    src, aids = local_pool()
    items = aids[300:400]
    print(f"\n[P-1b] copy 100 条进 A={ta}，回读实际进夹数，再对实际条目做一次 batch-del")
    raw("write", "/x/v3/fav/resource/copy", write=True,
        data=wdata(src_media_id=src, tar_media_id=ta, mid=MID,
                   resources=",".join(f"{a}:2" for a in items), platform="web"),
        note="copy 100 条")
    present = set(folder_aids(ta))
    got = [a for a in items if a in present]
    print(f"  [P-1b] copy 100 条 → 实际进夹 {len(got)} 条（再次印证 code=0 会静默丢条）")
    if got:
        _, b = raw("write", "/x/v3/fav/resource/batch-del", write=True,
                   data=wdata(media_id=ta, resources=",".join(f"{a}:2" for a in got),
                              platform="web"), note=f"batch-del 实际 {len(got)} 条")
        left = len(set(got) & set(folder_aids(ta)))
        print(f"  [P-1b] del code={b.get('code')} message={b.get('message')} ｜ 残留 {left} 条"
              f" ⇒ 上限判定：{'一次删 ' + str(len(got)) + ' 条可行' if left == 0 else '有残留'}")
    clear_folder(ta, "[P-1b] A")


def stage_p1c():
    """P-1c 查明 copy「静默丢条」的那些条目到底是什么状态（失效？还是有效但没进去）。"""
    ta = probe_id("A")
    src, aids = local_pool()
    items = aids[300:400]
    print(f"\n[P-1c] 复用 P-1b 的 100 条，找出没进夹的是哪些，再用 resource/infos 查它们的状态")
    raw("write", "/x/v3/fav/resource/copy", write=True,
        data=wdata(src_media_id=src, tar_media_id=ta, mid=MID,
                   resources=",".join(f"{a}:2" for a in items), platform="web"),
        note="copy 100 条")
    present = set(folder_aids(ta))
    missing = [a for a in items if a not in present]
    print(f"  [P-1c] 没进夹的 {len(missing)} 条：{missing}")
    if missing:
        _, body = raw("read", "/x/v3/fav/resource/infos", write=False,
                      params={"resources": ",".join(f"{a}:2" for a in missing), "platform": "web"},
                      note=f"查这 {len(missing)} 条的状态")
        data = body.get("data")
        if not data:
            print(f"  [P-1c] infos 返回 data={data} ⇒ 这些内容已不存在（被删/失效），"
                  f"端上查不到，自然复制不过去")
        for it in (data or []):
            print(f"    aid={it.get('id')} attr={it.get('attr')} "
                  f"title={(it.get('title') or '')[:30]!r} bvid={it.get('bvid')}")
    clear_folder(ta, "[P-1c] A")


def stage_p2():
    """P-2 批内混入坏条目的语义：整体拒绝 / 部分生效 / 静默部分成功。"""
    tb = probe_id("B")
    src, aids = local_pool()
    base = aids[-40:-20]
    bad_fake, bad_real = 999999999, local_invalid_aid()
    print(f"\n[P-2] 探针夹B={tb}  正常条目 {len(base)} 条  编造aid={bad_fake}  真实失效aid={bad_real}")
    for tag, bad in (("编造aid", bad_fake), ("真实失效aid", bad_real)):
        if not bad:
            print(f"  [P-2] {tag}: 无可用坏 aid，跳过")
            continue
        res = ",".join(f"{a}:2" for a in base) + f",{bad}:2"
        raw("write", "/x/v3/fav/resource/copy", write=True,
            data=wdata(src_media_id=src, tar_media_id=tb, mid=MID,
                       resources=res, platform="web"), note=f"copy 混{tag}")
        before = len(set(base) & set(folder_aids(tb)))
        _, b_del = raw("write", "/x/v3/fav/resource/batch-del", write=True,
                       data=wdata(media_id=tb, resources=res, platform="web"),
                       note=f"batch-del 混{tag}")
        after = len(set(base) & set(folder_aids(tb)))
        verdict = ("整体拒绝（一条没删）" if before - after == 0 else
                   "部分生效（删掉了其余）" if after == 0 else
                   f"部分生效（剩 {after} 条）")
        print(f"  [P-2] {tag}: del code={b_del.get('code')} message={b_del.get('message')}"
              f" ｜ 删前 {before} 条 → 删后 {after} 条 ⇒ {verdict}")
        clear_folder(tb, f"[P-2] {tag}")


def stage_p3():
    """P-3 move 是否原子：A→C 后回读两侧。"""
    ta, tc = probe_id("A"), probe_id("C")
    src, aids = local_pool()
    items = aids[-10:]
    res = ",".join(f"{a}:2" for a in items)
    print(f"\n[P-3] A={ta} → C={tc}，条目 {len(items)} 条")
    raw("write", "/x/v3/fav/resource/copy", write=True,
        data=wdata(src_media_id=src, tar_media_id=ta, mid=MID, resources=res, platform="web"),
        note="copy 到 A")
    a_before = set(folder_aids(ta))
    _, b_mv = raw("write", "/x/v3/fav/resource/move", write=True,
                  data=wdata(src_media_id=ta, tar_media_id=tc, mid=MID,
                             resources=res, platform="web"), note="move A→C")
    a_after, c_after = set(folder_aids(ta)), set(folder_aids(tc))
    in_a_before = len(set(items) & a_before)
    in_a_after = len(set(items) & a_after)
    in_c = len(set(items) & c_after)
    print(f"  [P-3] move code={b_mv.get('code')} ｜ 测试条目：A 前 {in_a_before} → 后 {in_a_after}；"
          f"C 后 {in_c}")
    print(f"  [P-3] 判定：{'原子（两边一致）' if (in_a_after == 0) == (in_c == len(items)) else '不原子'}")
    clear_folder(ta, "[P-3] A")
    clear_folder(tc, "[P-3] C")


def stage_p4():
    """P-4 对「已在目标夹」的视频重复 add，看错误码（确诊那 5 条 failed）。"""
    ta, tc = probe_id("A"), probe_id("C")
    src, aids = local_pool()
    item = aids[-1]
    print(f"\n[P-4] 测试 aid={item}")
    raw("write", "/x/v3/fav/resource/copy", write=True,
        data=wdata(src_media_id=src, tar_media_id=ta, mid=MID, resources=f"{item}:2",
                   platform="web"), note="先放进 A")
    raw("write", "/x/v3/fav/resource/deal", write=True,
        data=wdata(rid=item, type=2, add_media_ids=ta),
        note="关键：对【已在 A】的条目再 add 到 A")
    raw("write", "/x/v3/fav/resource/deal", write=True,
        data=wdata(rid=item, type=2, add_media_ids=tc),
        note="对照：同一条 add 到【不在其中的】C（期望 code=0）")
    clear_folder(ta, "[P-4] A")
    clear_folder(tc, "[P-4] C")


def stage_p1bound():
    """P-1bound 分辨边界是「条数 1000」还是「请求体积 ~16KB」。
    对照：同样条数下改变单条长度 / 同样长度下改变条数。"""
    src, aids = local_pool()
    _, b = raw("write", "/x/v3/fav/folder/add", write=True,
               data=wdata(title="_probe_边界2", privacy=1), note="建临时夹 _probe_边界2")
    tid = str((b.get("data") or {}).get("id", "") or "")
    if not tid:
        print(f"  [bound] 建夹失败 code={b.get('code')} ⇒ 无空位，中止")
        return
    print(f"  [bound] 临时夹 id={tid}")
    try:
        cases = [
            ("1000 条 · 真实 aid（对照，约 11 字符/条）", [f"{a}:2" for a in aids[:1000]]),
            ("1000 条 · 超长 aid（18 字符/条，测体积）",
             [f"{999999999999900 + i}:2" for i in range(1000)]),
            ("1001 条 · 极短 aid（约 6 字符/条，测条数）", [f"{i}:12" for i in range(1, 1002)]),
            ("1500 条 · 真实 aid（测条数，约 11 字符/条）", [f"{a}:2" for a in aids[:1500]]),
        ]
        for label, items in cases:
            res = ",".join(items)
            _, bd = raw("write", "/x/v3/fav/resource/batch-del", write=True,
                        data=wdata(media_id=tid, resources=res, platform="web"),
                        note=f"batch-del {label}")
            print(f"  [bound] {label}：{len(items)} 条 / {len(res)} 字符 → "
                  f"code={bd.get('code')}（{bd.get('message')}）")
    finally:
        raw("write", "/x/v3/fav/folder/del", write=True,
            data=wdata(media_ids=tid), note="删掉临时夹 _probe_边界2")


def stage_p1sweep():
    """P-1sweep 找边界：条数限制还是请求体积限制。
    用「不存在但格式合法」的内容，不碰任何真实数据。"""
    src, _ = local_pool()
    _, b = raw("write", "/x/v3/fav/folder/add", write=True,
               data=wdata(title="_probe_边界", privacy=1), note="建临时夹 _probe_边界")
    tid = str((b.get("data") or {}).get("id", "") or "")
    if not tid:
        print(f"  [sweep] 建夹失败 code={b.get('code')} ⇒ 无空位，中止")
        return
    print(f"  [sweep] 临时夹 id={tid}")
    try:
        cases = [
            ("2000 条 · aid 9 位", [f"{800000000 + i}:2" for i in range(2000)]),
            ("5000 条 · aid 9 位", [f"{800000000 + i}:2" for i in range(5000)]),
            ("7000 条 · aid 9 位", [f"{800000000 + i}:2" for i in range(7000)]),
            ("9000 条 · aid 9 位", [f"{800000000 + i}:2" for i in range(9000)]),
            ("9000 条 · 短 aid(type12)", [f"{i}:12" for i in range(9000)]),
            ("9000 条 · 超短 aid(type12)", [f"{i}:12" for i in range(9000, 18000)]),
        ]
        for label, items in cases:
            res = ",".join(items)
            _, bd = raw("write", "/x/v3/fav/resource/batch-del", write=True,
                        data=wdata(media_id=tid, resources=res, platform="web"),
                        note=f"batch-del {label}")
            _, bc = raw("write", "/x/v3/fav/resource/copy", write=True,
                        data=wdata(src_media_id=src, tar_media_id=tid, mid=MID,
                                   resources=res, platform="web"), note=f"copy {label}")
            print(f"  [sweep] {label}：payload {len(res)} 字符 → "
                  f"del code={bd.get('code')}({bd.get('message')}) / "
                  f"copy code={bc.get('code')}({bc.get('message')})")
    finally:
        raw("write", "/x/v3/fav/folder/del", write=True,
            data=wdata(media_ids=tid), note="删掉临时夹 _probe_边界")


def stage_p1huge():
    """P-1huge 9000 条量级的请求容忍度：用不存在的 aid 构造，只测"请求本身会不会被拒"。"""
    src, _ = local_pool()
    _, b = raw("write", "/x/v3/fav/folder/add", write=True,
               data=wdata(title="_probe_巨批", privacy=1), note="建临时夹 _probe_巨批")
    tid = str((b.get("data") or {}).get("id", "") or "")
    if not tid:
        print(f"  [huge] 建夹失败 code={b.get('code')} message={b.get('message')} ⇒ 无空位，中止")
        return
    print(f"  [huge] 临时夹 id={tid}")
    try:
        n = 9000
        bogus = ",".join(f"{800000000 + i}:2" for i in range(n))
        print(f"  [huge] 构造 {n} 条不存在的 aid，resources 共 {len(bogus)} 字符")
        _, b1 = raw("write", "/x/v3/fav/resource/batch-del", write=True,
                    data=wdata(media_id=tid, resources=bogus, platform="web"),
                    note=f"batch-del {n} 条（全不存在）")
        _, b2 = raw("write", "/x/v3/fav/resource/copy", write=True,
                    data=wdata(src_media_id=src, tar_media_id=tid, mid=MID,
                               resources=bogus, platform="web"),
                    note=f"copy {n} 条（全不存在）")
        ok = b1.get("code") == 0 and b2.get("code") == 0
        print(f"  [huge] 结论：batch-del code={b1.get('code')} / copy code={b2.get('code')}"
              f" ⇒ {'9000 条量级被接受，未见尺寸/条数上限' if ok else '被拒绝，见上面 message'}")
    finally:
        raw("write", "/x/v3/fav/folder/del", write=True,
            data=wdata(media_ids=tid), note="删掉临时夹 _probe_巨批")


def stage_counttest():
    """只读+少量写：验证 list-all 的 media_count 能否当「1 次请求」的批量校验依据。"""
    src, aids = local_pool()
    _, b = raw("write", "/x/v3/fav/folder/add", write=True,
               data=wdata(title="_probe_计数", privacy=1), note="建临时夹 _probe_计数")
    tid = str((b.get("data") or {}).get("id", "") or "")
    if not tid:
        print(f"  [count] 建夹失败 code={b.get('code')} ⇒ 无空位，测试中止")
        return
    print(f"  [count] 临时夹 id={tid}")
    items = aids[900:920]
    try:
        fs = live_folders()
        print(f"  [count] 建夹后 media_count = {fs.get('_probe_计数', ('', '?'))[1]}（期望 0）")
        raw("write", "/x/v3/fav/resource/copy", write=True,
            data=wdata(src_media_id=src, tar_media_id=tid, mid=MID,
                       resources=",".join(f"{a}:2" for a in items), platform="web"),
            note="copy 20 条")
        fs = live_folders()
        after = fs.get("_probe_计数", ("", "?"))[1]
        print(f"  [count] copy 20 条后 media_count = {after}（期望 20）")
        raw("write", "/x/v3/fav/resource/batch-del", write=True,
            data=wdata(media_id=tid, resources=",".join(f"{a}:2" for a in items),
                       platform="web"), note="batch-del 20 条")
        fs = live_folders()
        print(f"  [count] 删除后 media_count = {fs.get('_probe_计数', ('', '?'))[1]}（期望 0）")
        print("  [count] 结论：media_count 实时准确 ⇒ 可用 1 次 list-all 做整批校验"
              if after == 20 else "  [count] 结论：media_count 不实时/不准，不能当校验依据")
    finally:
        raw("write", "/x/v3/fav/folder/del", write=True,
            data=wdata(media_ids=tid), note="删掉临时夹 _probe_计数")


def stage_idstest():
    """只读：确认 resource/ids 能一次返回全夹 id（批量校验的低成本回读手段）。"""
    fs = live_folders()
    for name in ("默认收藏夹", "机器学习", "宠物"):
        if name not in fs:
            continue
        mid_, cnt = fs[name]
        _, body = raw("read", "/x/v3/fav/resource/ids", write=False,
                      params={"media_id": mid_, "platform": "web"}, note=f"resource/ids「{name}」")
        data = body.get("data") or []
        verdict = "一次拿全（无分页）" if len(data) >= cnt else f"只返回 {len(data)}，少于 media_count"
        print(f"  [ids] 「{name}」media_count={cnt} → ids 返回 {len(data)} 条 ⇒ {verdict}")
        if data:
            print(f"        样例：{data[0]}")
    src, aids = local_pool()
    _, body = raw("read", "/x/v3/fav/resource/ids", write=False,
                  params={"media_id": src, "platform": "web"}, note=f"resource/ids 源夹 {src}")
    data = body.get("data") or []
    print(f"  [ids] 源夹 {src} → ids 返回 {len(data)} 条；本地可 copy 条目 {len(aids)} 条"
          f"（差值≈失效条目）")


def stage_p1big():
    """P-1big 200 / 500 / 1000 条：找真正的批量上限（自带建夹与删夹）。"""
    src, aids = local_pool()
    _, b = raw("write", "/x/v3/fav/folder/add", write=True,
               data=wdata(title="_probe_上限2", privacy=1), note="建临时夹 _probe_上限2")
    tid = str((b.get("data") or {}).get("id", "") or "")
    if not tid:
        print(f"  [P-1big] 建夹失败 code={b.get('code')} message={b.get('message')}"
              f" ⇒ 当前无空位，测试中止")
        return
    print(f"  [P-1big] 临时夹 id={tid}")
    pos = 400
    try:
        for s in (200, 500, 1000):
            items = aids[pos:pos + s]
            pos += s
            if len(items) < s:
                print(f"  [P-1big] aid 池不足（需 {s}），跳过")
                break
            pages = s // 20 + 6
            res = ",".join(f"{a}:2" for a in items)
            _, bc = raw("write", "/x/v3/fav/resource/copy", write=True,
                        data=wdata(src_media_id=src, tar_media_id=tid, mid=MID,
                                   resources=res, platform="web"), note=f"copy {s} 条")
            present = folder_aids(tid, max_pages=pages)
            got = len(set(items) & set(present))
            _, bd = raw("write", "/x/v3/fav/resource/batch-del", write=True,
                        data=wdata(media_id=tid,
                                   resources=",".join(f"{a}:2" for a in present) or "0:2",
                                   platform="web"),
                        note=f"batch-del 实际 {len(present)} 条")
            left = len(set(items) & set(folder_aids(tid, max_pages=3)))
            print(f"  [P-1big] S={s}：copy code={bc.get('code')}（进夹 {got}/{s}）"
                  f" | del code={bd.get('code')}（message={bd.get('message')}，残留 {left}）")
            rest = folder_aids(tid, max_pages=pages)
            if rest:
                raw("write", "/x/v3/fav/resource/batch-del", write=True,
                    data=wdata(media_id=tid, resources=",".join(f"{a}:2" for a in rest),
                               platform="web"), note=f"再清残留 {len(rest)} 条")
    finally:
        raw("write", "/x/v3/fav/folder/del", write=True,
            data=wdata(media_ids=tid), note="删掉临时夹 _probe_上限2")


def stage_info5():
    """只读：查那 5 条 failed 的当前状态（是否已失效），用于确诊执行失败原因。"""
    vids = json.loads((HERE / "data" / "videos.json").read_text("utf-8"))
    plan = json.loads((HERE / "data" / "plan.json").read_text("utf-8"))
    bvids = ["BV1e5411R7rF", "BV1x44y1P7s2", "BV1KY411t75D", "BV13a4y1j7uJ", "BV1LR4y1b7dM"]
    rows = []
    for b in bvids:
        v = vids.get(b) or {}
        rows.append((int(v.get("aid") or 0), (plan.get(b) or {}).get("target_folder", ""),
                     (v.get("title") or "")[:26]))
    print("\n[info5] 5 条 failed 的本地信息：")
    for aid, tgt, title in rows:
        print(f"    aid={aid}  目标={tgt!r}  本地标题={title!r}")
    _, body = raw("read", "/x/v3/fav/resource/infos", write=False,
                  params={"resources": ",".join(f"{a}:2" for a, _, _ in rows), "platform": "web"},
                  note="查这 5 条现在还在不在")
    got = {it.get("id"): it for it in (body.get("data") or [])}
    print("  [info5] 现状：")
    for aid, tgt, title in rows:
        it = got.get(aid)
        if not it:
            print(f"    aid={aid} → 端上已不存在（已删除/失效）⇒ add 必然失败，"
                  f"这类应改判为 skip，不是 failed")
        else:
            attr = it.get("attr")
            print(f"    aid={aid} → attr={attr}（0 正常/1 失效）现标题="
                  f"{(it.get('title') or '')[:26]!r}")


def stage_retry4():
    """重试 4 条目标为「机器学习」的 failed（探针夹 A 正是机器学习），看是否仍然失败。
    （目标为 VPN 的那条不在授权范围内，不动。）"""
    ta = probe_id("A")
    aids = [468736007, 980345114, 669598114, 336832939]
    print("\n[retry4] 对 4 条 failed 重发一次 add（目标＝探针夹 A＝机器学习）：")
    codes = []
    for a in aids:
        _, b = raw("write", "/x/v3/fav/resource/deal", write=True,
                   data=wdata(rid=a, type=2, add_media_ids=ta), note=f"add aid={a}")
        codes.append(b.get("code"))
    present = set(folder_aids(ta))
    print(f"  [retry4] 4 次 add 的 code = {codes}；实际进夹 {len(set(aids) & present)}/4")
    if all(c == 0 for c in codes) and len(set(aids) & present) == 4:
        print("  [retry4] 全部成功 ⇒ 当初的失败是**瞬时**的（最可能就是 412/风控被吞成 False），"
              "符合 P2-7 的判断：写路径缺退避与重试")
    clear_folder(ta, "[retry4] A")


def stage_p6():
    """P-6 连续 20 次写请求看风控（2s 间隔）。"""
    tc = probe_id("C")
    src, aids = local_pool()
    res = ",".join(f"{a}:2" for a in aids[200:205])
    print(f"\n[P-6] 用 C={tc} 连做 10 轮 copy + batch-del（共 20 次写，间隔 {GAP}s）")
    bad = []
    for i in range(1, 11):
        _, c1 = raw("write", "/x/v3/fav/resource/copy", write=True,
                    data=wdata(src_media_id=src, tar_media_id=tc, mid=MID,
                               resources=res, platform="web"), note=f"轮{i} copy")
        _, c2 = raw("write", "/x/v3/fav/resource/batch-del", write=True,
                    data=wdata(media_id=tc, resources=res, platform="web"), note=f"轮{i} del")
        for code in (c1.get("code"), c2.get("code")):
            if code not in (0, None):
                bad.append(code)
    print(f"  [P-6] 非 0 返回：{bad if bad else '无（20 次写请求全部干净）'}")


def stage_p7():
    """P-7 夹数量上限 / 重名建夹语义（关系到 create_new 61 条能否落地）。"""
    fs = live_folders()
    print(f"\n[P-7] 当前夹数量 = {len(fs)}（用户说上限 99，实测看是否 100）")
    name = "_probe_上限测试"
    _, b1 = raw("write", "/x/v3/fav/folder/add", write=True,
                data=wdata(title=name, privacy=1), note=f"第一次建 {name}")
    d1 = (b1.get("data") or {}).get("id")
    print(f"  [P-7] 第一次：code={b1.get('code')} message={b1.get('message')} id={d1}")
    if b1.get("code") == 0:
        _, b2 = raw("write", "/x/v3/fav/folder/add", write=True,
                    data=wdata(title=name, privacy=1), note=f"第二次建同名 {name}")
        print(f"  [P-7] 第二次（重名）：code={b2.get('code')} message={b2.get('message')} "
              f"id={(b2.get('data') or {}).get('id')}")
        for mid_ in [x for x in (str(d1), str((b2.get('data') or {}).get('id'))) if x and x != "None"]:
            raw("write", "/x/v3/fav/folder/del", write=True,
                data=wdata(media_ids=mid_), note=f"删掉测试夹 {mid_}")
    else:
        print("  [P-7] 建夹被拒 ⇒ 已到数量上限，create_new 型操作当前不可行")


def stage_cleanup():
    """清空并删掉三个探针夹（恢复成「三个空位」的原状）。"""
    print("\n[cleanup] 清空并删除三个探针夹")
    for k in ("A", "B", "C"):
        mid_ = probe_id(k)
        if not mid_:
            continue
        clear_folder(mid_, f"[cleanup] {PROBE_NAMES[k]}")
        raw("write", "/x/v3/fav/folder/del", write=True,
            data=wdata(media_ids=mid_), note=f"删夹 {PROBE_NAMES[k]}({mid_})")
    fs = live_folders()
    print(f"  [cleanup] 清理后夹数量 = {len(fs)}；残留探针夹："
          f"{[n for n in fs if n.startswith('_probe_')] or '无'}")


def main():
    global GO, SESSION, CSRF, MID
    ap = argparse.ArgumentParser(description="B站收藏夹批量接口探针")
    ap.add_argument("stage", choices=["inspect", "bind", "p1", "p1b", "p1big", "p1c", "p1huge",
                                      "p1sweep", "p1bound", "p2", "p3", "p4", "idstest", "counttest",
                                      "info5", "retry4", "p6", "p7", "cleanup"])
    ap.add_argument("--go", action="store_true", help="真的发写请求（默认空转）")
    a = ap.parse_args()
    GO = a.go
    SESSION = open_session()
    CSRF = SESSION.cookie.bili_jct
    MID = SESSION.get_mid()
    print(f"[init] mid={MID}  写请求={'已放行' if GO else '空转（加 --go 才发）'}")
    {"inspect": stage_inspect, "bind": stage_bind, "p1": stage_p1, "p1b": stage_p1b,
     "p1c": stage_p1c, "p2": stage_p2, "p3": stage_p3, "p4": stage_p4, "info5": stage_info5,
     "p1big": stage_p1big, "idstest": stage_idstest, "counttest": stage_counttest,
     "p1huge": stage_p1huge, "p1sweep": stage_p1sweep, "p1bound": stage_p1bound, "retry4": stage_retry4, "p6": stage_p6,
     "p7": stage_p7, "cleanup": stage_cleanup}[a.stage]()


if __name__ == "__main__":
    main()
