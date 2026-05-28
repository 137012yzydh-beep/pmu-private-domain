#!/usr/bin/env python3
"""
CI 版：从环境变量读取 API Key，拉取全量训练数据并生成 train_report.html
用于 GitHub Actions 每日定时运行
"""
import json, os, re, ssl, sys
import urllib.request, gzip, time
from datetime import date, timedelta, datetime

API_KEY = os.environ.get("XUNJI_API_KEY", "")
if not API_KEY:
    print("ERROR: XUNJI_API_KEY 环境变量未设置")
    sys.exit(1)

BASE_URL = "https://trains.xunjiapp.cn"

# 解决 GitHub Actions UTC 时区导致日期少一天的问题（强制转为北京时间）
utc_now = datetime.utcnow()
beijing_now = utc_now + timedelta(hours=8)
today = beijing_now.date()

start = date(2026, 4, 1)
days_count = (today - start).days + 1

print(f"从 {start} 到 {today}，共 {days_count} 天，拉取中... (北京时间基准)")
all_data = {}

for i in range(days_count):
    d = start + timedelta(days=i)
    day_str = d.strftime("%Y-%m-%d")

    url = f"{BASE_URL}/api_trains_for_llm"
    payload = json.dumps({"datestr": day_str}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {API_KEY}")
    req.add_header("Content-Type", "application/json")

    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            raw = resp.read()
            try:
                content = gzip.decompress(raw).decode("utf-8")
            except:
                content = raw.decode("utf-8")
        data = json.loads(content)
        records = data.get("res", [])
        if isinstance(records, list) and len(records) > 0:
            all_data[day_str] = records
            wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][d.weekday()]
            print(f"  {day_str} {wd}: {len(records)}条")
        elif "too frequent" in str(data.get("res", "")):
            print(f"  {day_str}: 频率限制，等30秒...")
            time.sleep(30)
    except Exception as e:
        print(f"  {day_str}: 错误 {e}")

    time.sleep(0.3)

print(f"\n共获取 {len(all_data)} 天数据")


# ═══════════════════════════════════════════
# 解析器（优化正则与容错）
# ═══════════════════════════════════════════

def parse_cardio(record_str):
    parts = [p.strip() for p in record_str.split(",")]
    result = {"type": "", "kcal": 0, "time": 0, "hr": 0, "dist": 0}
    for p in parts:
        if p == "有氧" or p.startswith("id:"):
            continue
        if p.endswith("kcal"):
            try:
                result["kcal"] = int(p.replace("kcal", ""))
            except:
                pass
        elif p.endswith("bpm"):
            try:
                result["hr"] = int(p.replace("bpm", ""))
            except:
                pass
        elif p.startswith("time:"):
            try:
                result["time"] = int(p.replace("time:", "").replace("s", ""))
            except:
                pass
        elif p.endswith("km"):
            try:
                result["dist"] = float(p.replace("km", ""))
            except:
                pass
        # 允许序号前有空格，例如 " 1.户外骑行"
        elif re.match(r"^\s*\d+\.", p):
            result["type"] = re.sub(r"^\s*\d+\.", "", p)
    if not result["type"] and result["dist"]:
        result["type"] = "有氧"
    return result


def parse_strength(record_str):
    parts = [p.strip() for p in record_str.split(",")]
    name, calorie, train_time = "", 0, 0
    header_done = False
    current_ex, current_sets = None, []
    exercises = []
    pending_wt, pending_rep = "", ""

    for p in parts[1:]:
        if not header_done:
            if p.startswith("id:"):
                continue
            elif p.startswith("train_time:"):
                ts = p.split(":", 1)[1]
                if "-" in ts:
                    a, b = ts.split("-")
                    train_time = (int(b) - int(a)) // 1000
                continue
            elif p.startswith("calorie:"):
                calorie = int(p.split(":", 1)[1])
                header_done = True
                continue
            # 不是数字序号开头，且不为空 — 这是训练计划名称
            elif not re.match(r"^\s*\d+\.", p) and p != "":
                name = p
                continue

        if re.match(r"^[\d.]+kg$", p):
            pending_wt = p
            continue
        if re.match(r"^[\d.]+次$", p):
            pending_rep = p
            current_sets.append({"wt": pending_wt, "rep": pending_rep})
            pending_wt, pending_rep = "", ""
            continue
        if re.match(r"^\d+组$", p):
            if pending_wt or pending_rep:
                current_sets.append({"wt": pending_wt, "rep": pending_rep})
            pending_wt, pending_rep = "", ""
            continue
        if re.match(r"^time:\d+s$", p):
            continue
        # 允许序号前有空格
        if re.match(r"^\s*\d+\.", p):
            if current_ex and current_sets:
                exercises.append({"name": current_ex, "sets": current_sets})
            current_ex = re.sub(r"^\s*\d+\.", "", p)
            current_sets = []
            pending_wt, pending_rep = "", ""
            continue

    if current_ex:
        if pending_wt or pending_rep:
            current_sets.append({"wt": pending_wt, "rep": pending_rep})
        if current_sets:
            exercises.append({"name": current_ex, "sets": current_sets})

    return {"name": name, "calorie": calorie, "time": train_time, "exercises": exercises}


# ═══════════════════════════════════════════
# 汇总 & 去重
# ═══════════════════════════════════════════

parsed_data = {}
for date_str, records in sorted(all_data.items()):
    daily = {"cardio": [], "strength": []}
    for rec in records:
        if "有氧" in rec:
            daily["cardio"].append(parse_cardio(rec))
        else:
            parsed = parse_strength(rec)
            if parsed["exercises"]:
                daily["strength"].append(parsed)
    # 去重
    seen = set()
    unique_cardio = []
    for c in daily["cardio"]:
        key = (c["type"], c["kcal"], c["time"])
        if key not in seen:
            seen.add(key)
            unique_cardio.append(c)
    daily["cardio"] = unique_cardio
    if daily["cardio"] or daily["strength"]:
        parsed_data[date_str] = daily

dates = sorted(parsed_data.keys())
if not dates:
    print("WARNING: 没有拉取到任何有效训练数据！")
    sys.exit(0)

total_strength = sum(len(d["strength"]) for d in parsed_data.values())
total_cardio = sum(len(d["cardio"]) for d in parsed_data.values())
first_date = dates[0]
last_date = dates[-1]

print(f"\n--- 汇总 ---")
print(f"总训练天数: {len(dates)}")
print(f"力量: {total_strength} | 有氧: {total_cardio}")
print(f"数据范围: {first_date} → {last_date}")


# ═══════════════════════════════════════════
# 生成 JS 对象数据字符串
# ═══════════════════════════════════════════

def escape_js(s):
    return s.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"').replace("\n", "\\n")


js_lines = ["{"]
for date_str in dates:
    d = parsed_data[date_str]
    entries = []
    if d["cardio"]:
        cardio_entries = []
        for c in d["cardio"]:
            dist_part = f',dist:{c["dist"]}' if c.get("dist") else ""
            cardio_entries.append(
                f'{{type:"{escape_js(c["type"])}",kcal:{c["kcal"]},time:{c["time"]},hr:{c["hr"]}'
                + dist_part
                + "}"
            )
        entries.append("cardio:[" + ",".join(cardio_entries) + "]")
    else:
        entries.append("cardio:null")

    if d["strength"]:
        str_entries = []
        for s in d["strength"]:
            ex_lines = []
            for ex in s["exercises"]:
                set_lines = []
                for st in ex["sets"]:
                    set_lines.append(
                        f'{{wt:"{escape_js(st["wt"])}",rep:"{escape_js(st["rep"])}"}}'
                    )
                ex_lines.append(
                    f'{{name:"{escape_js(ex["name"])}",sets:[{",".join(set_lines)}]}}'
                )
            str_entries.append(
                f'{{name:"{escape_js(s["name"])}",kcal:{s["calorie"]},time:{s["time"]},exercises:[{",".join(ex_lines)}]}}'
            )
        entries.append(f'strength:[{",".join(str_entries)}]')
    else:
        entries.append("strength:null")

    js_lines.append(f'  "{date_str}": {{{",".join(entries)}}},')

js_lines.append("}")
js_data_string = "\n".join(js_lines)


# ═══════════════════════════════════════════
# HTML 模板（使用普通字符串 + .replace()，避免 f-string 大括号冲突）
# ═══════════════════════════════════════════

html_template = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>训记训练数据 · 全量汇总</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#0f0f0f; color:#e0e0e0; padding:24px; max-width:1300px; margin:0 auto; }
h1 { font-size:24px; margin-bottom:4px; }
.sub { color:#888; font-size:14px; margin-bottom:24px; transition:color .3s; }

.floating-red {
  position:fixed; top:24px; right:24px; z-index:200;
  width:36px; height:36px; border-radius:50%; background:#ff5f57;
  border:none; cursor:pointer; opacity:0; transform:scale(0); pointer-events:none;
  transition: opacity .4s, transform .4s cubic-bezier(.34,1.56,.64,1);
  box-shadow: 0 0 18px rgba(255,95,87,.4);
}
.floating-red.show { opacity:1; transform:scale(1); pointer-events:auto; }
.floating-red:hover { transform:scale(1.2); box-shadow: 0 0 24px rgba(255,95,87,.6); }
.floating-red::after { content:'\01F534'; font-size:14px; position:absolute; inset:0; display:flex; align-items:center; justify-content:center; }

.card { background:#1a1a1a; border-radius:12px; padding:20px; margin-bottom:16px; border:1px solid #2a2a2a; position:relative; transition: opacity .5s, transform .5s, max-height .5s, padding .5s, margin .5s; overflow:hidden; }
.card.collapsed { opacity:0; transform:translateY(-20px); max-height:0; padding-top:0; padding-bottom:0; margin-bottom:0; border-width:0; }

.card-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:14px; }
.card-header h2 { font-size:16px; color:#aaa; margin:0; }
.header-right { display:flex; align-items:center; gap:10px; }

.week-nav { display:flex; align-items:center; gap:6px; }
.week-nav button { background:#2a2a2a; border:none; color:#aaa; width:26px;height:26px; border-radius:6px; cursor:pointer; font-size:14px; display:flex; align-items:center; justify-content:center; transition:all .2s; }
.week-nav button:hover:not(:disabled) { background:#3a3a3a; color:#fff; }
.week-nav button:disabled { opacity:.3; cursor:default; }
.week-label { font-size:12px; color:#888; min-width:100px; text-align:center; user-select:none; }

.traffic-lights { display:flex; gap:8px; align-items:center; }
.traffic-dot { width:13px; height:13px; border-radius:50%; cursor:pointer; transition:all .25s; position:relative; }
.traffic-dot::after { content:''; position:absolute; inset:0; border-radius:50%; opacity:0; transition:opacity .2s; }
.traffic-dot:hover::after { opacity:1; }
.traffic-dot.red { background:#ff5f57; }
.traffic-dot.red::after { box-shadow:0 0 10px rgba(255,95,87,.5); }
.traffic-dot.yellow { background:#febc2e; }
.traffic-dot.yellow::after { box-shadow:0 0 10px rgba(254,188,46,.5); }
.traffic-dot.green { background:#28c840; }
.traffic-dot.green::after { box-shadow:0 0 10px rgba(40,200,64,.5); }
.traffic-dot:hover { transform:scale(1.15); }

.stat-row { display:flex; gap:16px; flex-wrap:wrap; }
.stat { background:#222; border-radius:8px; padding:16px; flex:1; min-width:110px; text-align:center; transition:all .4s; }
.stat-label { font-size:12px; color:#888; margin-bottom:4px; }
.stat-value { font-size:28px; font-weight:700; transition:all .4s; }
.stat-unit { font-size:14px; color:#888; margin-left:2px; }
.val-red { color:#f44336; }
.val-green { color:#4caf50; }
.val-orange { color:#ff6b35; }

.week-grid { display:grid; grid-template-columns:repeat(7,1fr); gap:8px; }
.day { background:#222; border-radius:8px; padding:12px 8px; text-align:center; min-height:90px; transition:all .4s; cursor:default; }
.day.today { border:2px solid #ff6b35; }
.day.cardio { background:#1a2a1a; border:1px solid #2d5a2d; }
.day.strength { background:#2a1a1a; border:1px solid #5a2d2d; }
.day.both { background:linear-gradient(135deg, #1a2a1a 50%, #2a1a1a 50%); border:1px solid #444; }
.day.empty { color:#555; }
.day-name { font-size:12px; color:#888; margin-bottom:4px; }
.day-date { font-size:11px; color:#666; margin-bottom:6px; }
.day-tag { font-size:10px; padding:2px 6px; border-radius:4px; display:inline-block; margin:2px; }
.tag-cardio { background:#1a3a1a; color:#4caf50; }
.tag-strength { background:#3a1a1a; color:#f44336; }
.day-detail { font-size:11px; color:#ccc; line-height:1.5; margin-top:4px; }

.exercise-card { background:#222; border-radius:8px; padding:16px; margin-bottom:10px; border-left:3px solid #f44336; }
.ex-name { font-size:15px; font-weight:600; margin-bottom:6px; color:#f44336; }
.sets-grid { display:flex; flex-wrap:wrap; gap:6px; }
.set-badge { background:#2a2a2a; border-radius:6px; padding:6px 10px; font-size:12px; color:#ccc; }
.set-badge .wt { color:#ff9800; font-weight:600; }
.set-badge .rp { color:#4caf50; }

.warn { background:#2a1a1a; border:1px solid #5a2d2d; border-radius:8px; padding:12px; font-size:13px; color:#f44336; line-height:1.8; }
.empty-state { text-align:center; padding:40px 20px; color:#555; }
.empty-state .icon { font-size:48px; margin-bottom:12px; }

.detail-card { background:#1a1a1a; border-radius:12px; padding:20px; margin-bottom:16px; border:1px solid #2a2a2a; }
.detail-card.cardio-border { border-left:3px solid #4caf50; }
.detail-card.strength-border { border-left:3px solid #f44336; }

/* ═══ 三栏布局 ═══ */
.triple-panel { display:flex; gap:16px; margin-top:16px; }
.triple-col { flex:1; min-width:0; }
.triple-col h3 { font-size:14px; color:#888; margin-bottom:12px; padding-bottom:8px; border-bottom:1px solid #2a2a2a; }
.triple-col.train-col h3 { color:#f44336; }
.triple-col.sleep-col h3 { color:#28c840; }
.triple-col.diet-col h3 { color:#febc2e; }

.panel-card { background:#222; border-radius:8px; padding:12px; margin-bottom:8px; font-size:12px; color:#ccc; }
.panel-card .panel-date { font-size:11px; color:#666; margin-bottom:6px; }
.panel-card.train-panel { border-left:3px solid #f44336; }
.panel-card.sleep-panel { border-left:3px solid #28c840; }
.panel-card.diet-panel { border-left:3px solid #febc2e; }

.sleep-info { display:flex; gap:12px; align-items:center; }
.sleep-info .sleep-block { text-align:center; }
.sleep-info .sleep-block .sleep-val { font-size:18px; font-weight:700; color:#28c840; }
.sleep-info .sleep-block .sleep-lbl { font-size:10px; color:#666; }
.sleep-info .sleep-divider { color:#444; font-size:20px; }
.sleep-duration { color:#28c840; font-weight:600; }

/* 睡眠阶段小条（三栏用） */
.sleep-stages-mini { display:flex; gap:2px; margin-top:4px; height:8px; border-radius:4px; overflow:hidden; }
.sleep-stages-mini span { height:100%; border-radius:1px; }

/* ═══ 弹窗 ═══ */
.modal-overlay { position:fixed; inset:0; background:rgba(0,0,0,.7); z-index:100; display:flex; align-items:center; justify-content:center; opacity:0; pointer-events:none; transition:opacity .3s; }
.modal-overlay.show { opacity:1; pointer-events:auto; }
.modal { background:#1a1a1a; border:1px solid #2a2a2a; border-radius:12px; width:640px; max-height:88vh; overflow-y:auto; padding:24px; position:relative; }
.modal h2 { font-size:18px; margin-bottom:16px; }
.modal.slp-modal h2 { color:#28c840; }
.modal.diet-modal h2 { color:#febc2e; }
.modal-close { position:absolute; top:12px; right:16px; background:none; border:none; color:#888; font-size:20px; cursor:pointer; }
.modal-close:hover { color:#fff; }

/* 左上角日历选择器 */
.date-picker-top { display:flex; align-items:center; gap:8px; margin-bottom:16px; }
.date-picker-top input[type="date"] { background:#222; border:1px solid #444; color:#e0e0e0; padding:6px 10px; border-radius:6px; font-size:13px; outline:none; }
.date-picker-top input[type="date"]:focus { border-color:#28c840; }
.date-picker-top input[type="date"]::-webkit-calendar-picker-indicator { filter:invert(1); cursor:pointer; }
.date-picker-top label { font-size:12px; color:#888; }

/* 密码输入 */
.pwd-box { text-align:center; padding:20px; }
.pwd-box input { background:#222; border:1px solid #444; color:#e0e0e0; padding:10px 16px; border-radius:8px; font-size:16px; width:200px; text-align:center; outline:none; }
.pwd-box input:focus { border-color:#28c840; }
.pwd-box button { background:#28c840; border:none; color:#000; padding:10px 24px; border-radius:8px; font-size:14px; font-weight:600; cursor:pointer; margin-top:8px; }
.pwd-box .pwd-error { color:#f44336; font-size:12px; margin-top:8px; }

/* 编辑表单 */
.edit-form { display:none; }
.edit-form.show { display:block; }
.form-group { margin-bottom:16px; }
.form-group label { display:block; font-size:12px; color:#888; margin-bottom:4px; }
.form-group input, .form-group select { width:100%; background:#222; border:1px solid #444; color:#e0e0e0; padding:8px 12px; border-radius:6px; font-size:14px; outline:none; }
.form-group input:focus, .form-group select:focus { border-color:#28c840; }
.form-row { display:flex; gap:12px; }
.form-row .form-group { flex:1; }
.form-actions { display:flex; gap:8px; margin-top:16px; }
.form-actions button { flex:1; padding:10px; border:none; border-radius:8px; font-size:14px; font-weight:600; cursor:pointer; }
.btn-save { background:#28c840; color:#000; }
.btn-save-diet { background:#febc2e; color:#000; }
.btn-delete { background:#f44336; color:#fff; }
.btn-cancel { background:#333; color:#aaa; }

/* 睡眠阶段输入框颜色标记 */
.stage-input-awake { border-left:3px solid #febc2e !important; }
.stage-input-rem { border-left:3px solid #64b5f6 !important; }
.stage-input-light { border-left:3px solid #42a5f5 !important; }
.stage-input-deep { border-left:3px solid #1e88e5 !important; }

/* 饼状图区域 */
.pie-container { display:flex; gap:20px; align-items:center; margin-top:16px; padding:16px; background:#222; border-radius:8px; }
.pie-svg { flex-shrink:0; }
.pie-legend { font-size:12px; color:#ccc; }
.pie-legend .legend-item { display:flex; align-items:center; gap:6px; margin-bottom:6px; }
.pie-legend .legend-dot { width:10px; height:10px; border-radius:50%; flex-shrink:0; }
.pie-legend .legend-val { font-weight:600; margin-left:auto; }
.legend-awake .legend-dot { background:#febc2e; }
.legend-rem .legend-dot { background:#64b5f6; }
.legend-light .legend-dot { background:#42a5f5; }
.legend-deep .legend-dot { background:#1e88e5; }

/* 饮食表单 */
.meal-card { background:#222; border-radius:8px; padding:14px; margin-bottom:10px; border-left:3px solid #febc2e; position:relative; }
.meal-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }
.meal-name { font-size:14px; font-weight:600; color:#febc2e; }
.meal-kcal { font-size:13px; color:#ff6b35; font-weight:600; }
.meal-items { font-size:12px; color:#aaa; line-height:1.7; }
.macro-row { display:flex; gap:12px; margin-top:8px; font-size:11px; }
.macro-tag { padding:2px 8px; border-radius:4px; }
.macro-protein { background:#2a1a2a; color:#ce93d8; }
.macro-fat { background:#2a2a1a; color:#fff176; }
.macro-carb { background:#1a2a2a; color:#80cbc4; }

.btn-add { background:#333; border:1px dashed #555; color:#aaa; padding:8px 16px; border-radius:8px; cursor:pointer; font-size:13px; width:100%; margin-top:8px; transition:all .2s; }
.btn-add:hover { background:#3a3a3a; border-color:#888; color:#fff; }

.del-meal { position:absolute; top:8px; right:12px; background:none; border:none; color:#666; font-size:14px; cursor:pointer; }
.del-meal:hover { color:#f44336; }

.diet-summary { display:flex; gap:12px; margin-bottom:20px; }
.diet-stat { flex:1; background:#222; border-radius:8px; padding:12px; text-align:center; }
.diet-stat .label { font-size:11px; color:#888; }
.diet-stat .value { font-size:20px; font-weight:700; margin-top:4px; }

/* 睡眠记录卡片（弹窗内查看） */
.sleep-record-card { background:#222; border-radius:8px; padding:12px; margin-bottom:8px; border-left:3px solid #28c840; display:flex; justify-content:space-between; align-items:center; }
.sleep-record-card .sr-info { flex:1; }
.sleep-record-card .sr-date { font-size:11px; color:#666; margin-bottom:4px; }
.sleep-record-card .sr-detail { font-size:13px; color:#ccc; }
.sleep-record-card .sr-stages { display:flex; gap:8px; margin-top:4px; font-size:11px; }
.sleep-record-card .sr-stages span { padding:1px 6px; border-radius:3px; }
.sleep-record-card .sr-actions { display:flex; gap:6px; }
.sleep-record-card .sr-actions button { background:#333; border:none; color:#888; padding:4px 10px; border-radius:4px; cursor:pointer; font-size:11px; }
.sleep-record-card .sr-actions button:hover { color:#fff; }
.sr-edit:hover { background:#28c84033 !important; color:#28c840 !important; }
.sr-del:hover { background:#f4433633 !important; color:#f44336 !important; }
.stage-tag-awake { background:#febc2e22; color:#febc2e; }
.stage-tag-rem { background:#64b5f622; color:#64b5f6; }
.stage-tag-light { background:#42a5f522; color:#42a5f5; }
.stage-tag-deep { background:#1e88e522; color:#1e88e5; }
</style>
</head>
<body>

<h1>\01F3CB\0FE0F 训记训练数据</h1>
<p class="sub" id="weekSub"></p>

<button class="floating-red" id="floatingRed" onclick="restoreCards()" title="恢复面板"></button>

<div class="card" id="overviewCard">
  <div class="card-header">
    <h2>\01F4CA 本周概览</h2>
    <div class="header-right">
      <div class="week-nav">
        <button onclick="prevWeek()" id="btnPrev" title="上一周">\025C2</button>
        <span class="week-label" id="weekLabel"></span>
        <button onclick="nextWeek()" id="btnNext" title="下一周">\025B8</button>
      </div>
      <div class="traffic-lights">
        <div class="traffic-dot red" title="隐藏面板" onclick="hideCards()"></div>
        <div class="traffic-dot yellow" title="饮食记录" onclick="openDiet()"></div>
        <div class="traffic-dot green" title="睡眠记录" onclick="openSleep()"></div>
      </div>
    </div>
  </div>
  <div class="stat-row" id="statsRow"></div>
</div>

<div class="card" id="weekViewCard">
  <div class="card-header"><h2>\01F4C5 周视图</h2></div>
  <div class="week-grid" id="weekGrid"></div>
</div>

<div class="triple-panel" id="triplePanel">
  <div class="triple-col train-col">
    <h3>\01F3CB\0FE0F 训练记录</h3>
    <div id="trainCol"></div>
  </div>
  <div class="triple-col sleep-col">
    <h3>\01F634 睡眠记录</h3>
    <div id="sleepCol"></div>
  </div>
  <div class="triple-col diet-col">
    <h3>\01F37D\0FE0F 饮食记录</h3>
    <div id="dietCol"></div>
  </div>
</div>

<div id="detailSection"></div>
<div class="warn" id="analysisBox"></div>

<div class="modal-overlay" id="sleepModal">
  <div class="modal slp-modal">
    <button class="modal-close" onclick="closeSleep()">\02715</button>
    <h2>\01F634 睡眠记录</h2>
    <div id="sleepPwdBox" class="pwd-box">
      <p style="color:#888;font-size:13px;margin-bottom:12px;">输入密码以管理睡眠记录</p>
      <input type="password" id="sleepPwdInput" placeholder="输入密码" onkeydown="if(event.key==='Enter')verifySleepPwd()">
      <br><button onclick="verifySleepPwd()">验证</button>
      <div class="pwd-error" id="sleepPwdErr"></div>
    </div>
    <div class="edit-form" id="sleepEditForm">
      <div class="date-picker-top">
        <label>\01F4C5 日期：</label>
        <input type="date" id="sleepDatePicker" onchange="onSleepDateChange()">
      </div>
      <div id="sleepRecordsList"></div>
      <div id="sleepAddForm" style="display:none;">
        <div class="form-row">
          <div class="form-group"><label>入睡时间</label><input type="time" id="sBedTime"></div>
          <div class="form-group"><label>起床时间</label><input type="time" id="sWakeTime"></div>
        </div>
        <div style="font-size:12px;color:#888;margin-bottom:8px;">\01F4A4 睡眠阶段（分钟）</div>
        <div class="form-row">
          <div class="form-group"><label style="color:#febc2e;">清醒</label><input type="number" id="sAwake" class="stage-input-awake" placeholder="0" min="0" oninput="updateSleepPie()"></div>
          <div class="form-group"><label style="color:#64b5f6;">快速眼动 (REM)</label><input type="number" id="sRem" class="stage-input-rem" placeholder="0" min="0" oninput="updateSleepPie()"></div>
        </div>
        <div class="form-row">
          <div class="form-group"><label style="color:#42a5f5;">浅睡</label><input type="number" id="sLight" class="stage-input-light" placeholder="0" min="0" oninput="updateSleepPie()"></div>
          <div class="form-group"><label style="color:#1e88e5;">深睡</label><input type="number" id="sDeep" class="stage-input-deep" placeholder="0" min="0" oninput="updateSleepPie()"></div>
        </div>
        <div class="pie-container" id="sleepPieContainer">
          <svg class="pie-svg" id="sleepPieSvg" width="140" height="140" viewBox="0 0 140 140"></svg>
          <div class="pie-legend" id="sleepPieLegend"></div>
        </div>
        <div style="font-size:11px;color:#666;margin-top:4px;" id="sleepTotalLabel"></div>
        <div class="form-group" style="margin-top:12px;"><label>备注（可选）</label><input type="text" id="sNote" placeholder="如：中途醒了2次"></div>
        <div class="form-actions">
          <button class="btn-save" onclick="saveSleepRecord()">保存</button>
          <button class="btn-cancel" onclick="cancelSleepAdd()">取消</button>
        </div>
      </div>
      <button class="btn-add" id="btnAddSleep" onclick="showSleepAddForm()">+ 新增睡眠记录</button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="dietModal">
  <div class="modal diet-modal">
    <button class="modal-close" onclick="closeDiet()">\02715</button>
    <h2>\01F37D\0FE0F 饮食记录</h2>
    <div id="dietPwdBox" class="pwd-box">
      <p style="color:#888;font-size:13px;margin-bottom:12px;">输入密码以管理饮食记录</p>
      <input type="password" id="dietPwdInput" placeholder="输入密码" onkeydown="if(event.key==='Enter')verifyDietPwd()">
      <br><button onclick="verifyDietPwd()">验证</button>
      <div class="pwd-error" id="dietPwdErr"></div>
    </div>
    <div class="edit-form" id="dietEditForm">
      <div class="date-picker-top">
        <label>\01F4C5 日期：</label>
        <input type="date" id="dietDatePicker" onchange="onDietDateChange()">
      </div>
      <div id="dietDaySummary"></div>
      <div id="dietMealsList"></div>
      <div id="dietAddForm" style="display:none;">
        <div class="form-group"><label>餐次名称</label><input type="text" id="dMealName" placeholder="如：早餐、午餐、练后餐"></div>
        <div class="form-group"><label>食物列表</label><input type="text" id="dItems" placeholder="用逗号分隔，如：鸡胸肉200g, 米饭300g"></div>
        <div class="form-row">
          <div class="form-group"><label>热量 (kcal)</label><input type="number" id="dKcal" placeholder="500"></div>
          <div class="form-group"><label>蛋白质 (g)</label><input type="number" id="dProtein" placeholder="40"></div>
        </div>
        <div class="form-row">
          <div class="form-group"><label>脂肪 (g)</label><input type="number" id="dFat" placeholder="10"></div>
          <div class="form-group"><label>碳水 (g)</label><input type="number" id="dCarb" placeholder="60"></div>
        </div>
        <div class="form-actions">
          <button class="btn-save-diet" onclick="saveDietMeal()">保存</button>
          <button class="btn-cancel" onclick="cancelDietAdd()">取消</button>
        </div>
      </div>
      <button class="btn-add" id="btnAddDiet" onclick="showDietAddForm()">+ 新增餐食</button>
    </div>
  </div>
</div>

<script>
// ═══ 全量训练数据 ═══
const TRAINING_DATA = __JS_DATA_PLACEHOLDER__;

// ═══ localStorage 持久化 ═══
const STORAGE_KEY_SLEEP = 'pmu_sleep_data_v2';
const STORAGE_KEY_DIET = 'pmu_diet_data';
const AUTH_PASSWORD = '137012';

// 睡眠阶段配色
const STAGE_COLORS = {
  awake: '#febc2e',
  rem: '#64b5f6',
  light: '#42a5f5',
  deep: '#1e88e5'
};
const STAGE_LABELS = { awake: '清醒', rem: '快速眼动', light: '浅睡', deep: '深睡' };
const STAGE_ORDER = ['awake','rem','light','deep'];

let SLEEP_DATA = {};
let DIET_DATA = {};

function loadLocalData() {
  try { SLEEP_DATA = JSON.parse(localStorage.getItem(STORAGE_KEY_SLEEP)) || {}; } catch(e) { SLEEP_DATA = {}; }
  try { DIET_DATA = JSON.parse(localStorage.getItem(STORAGE_KEY_DIET)) || {}; } catch(e) { DIET_DATA = {}; }
}
function saveSleepData() { localStorage.setItem(STORAGE_KEY_SLEEP, JSON.stringify(SLEEP_DATA)); }
function saveDietData() { localStorage.setItem(STORAGE_KEY_DIET, JSON.stringify(DIET_DATA)); }

// ═══ 工具函数 ═══
function fmtDate(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return y + '-' + m + '-' + day;
}
function getMonday(d) {
  const m = new Date(d);
  const day = m.getDay();
  const diff = day === 0 ? -6 : 1 - day;
  m.setDate(m.getDate() + diff);
  m.setHours(0,0,0,0);
  return m;
}
function fmtMD(d) { return (d.getMonth()+1)+"/"+d.getDate(); }
const WEEKDAYS = ["周一","周二","周三","周四","周五","周六","周日"];
function getWeekDays(monday) {
  const days = [];
  for (let i=0;i<7;i++) { const d=new Date(monday); d.setDate(d.getDate()+i); days.push(d); }
  return days;
}
function secToHMS(s) {
  if (!s) return "0:00";
  const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;
  return h>0?h+":"+String(m).padStart(2,'0')+":"+String(sec).padStart(2,'0'):m+":"+String(sec).padStart(2,'0');
}
function sleepDuration(bed, wake) {
  if (!bed || !wake) return '\u2014';
  const [bh,bm] = bed.split(':').map(Number);
  const [wh,wm] = wake.split(':').map(Number);
  let mins = (wh*60+wm) - (bh*60+bm);
  if (mins < 0) mins += 24*60;
  const h = Math.floor(mins/60);
  const m = mins%60;
  return h+'h'+m+'m';
}
function minsToHM(mins) {
  if (!mins || mins <= 0) return '0m';
  const h = Math.floor(mins/60);
  const m = mins%60;
  return h>0 ? h+'h'+m+'m' : m+'m';
}

// ═══ SVG 饼状图 ═══
function drawPieChart(svgId, legendId, totalLabelId, awake, rem, light, deep) {
  const stages = [
    { key:'awake', val: awake, color: STAGE_COLORS.awake, label: STAGE_LABELS.awake },
    { key:'rem', val: rem, color: STAGE_COLORS.rem, label: STAGE_LABELS.rem },
    { key:'light', val: light, color: STAGE_COLORS.light, label: STAGE_LABELS.light },
    { key:'deep', val: deep, color: STAGE_COLORS.deep, label: STAGE_LABELS.deep }
  ];
  const total = awake + rem + light + deep;

  const totalEl = document.getElementById(totalLabelId);
  if (totalEl) totalEl.textContent = '\u603B\u7761\u7720\u65F6\u957F\uFF1A' + minsToHM(total);

  const svg = document.getElementById(svgId);
  const cx=70, cy=70, r=60;
  let svgInner = '';

  if (total === 0) {
    svgInner = '<circle cx="70" cy="70" r="60" fill="#333"/><text x="70" y="74" text-anchor="middle" fill="#666" font-size="13">\u65E0\u6570\u636E</text>';
  } else {
    let cumulative = -Math.PI/2;
    stages.forEach(s => {
      if (s.val <= 0) return;
      const slice = (s.val / total) * Math.PI * 2;
      const x1 = cx + r * Math.cos(cumulative);
      const y1 = cy + r * Math.sin(cumulative);
      const x2 = cx + r * Math.cos(cumulative + slice);
      const y2 = cy + r * Math.sin(cumulative + slice);
      const largeArc = slice > Math.PI ? 1 : 0;
      svgInner += '<path d="M'+cx+','+cy+' L'+x1+','+y1+' A'+r+','+r+' 0 '+largeArc+',1 '+x2+','+y2+' Z" fill="'+s.color+'"/>';
      cumulative += slice;
    });
    svgInner += '<circle cx="70" cy="70" r="32" fill="#222"/>';
    svgInner += '<text x="70" y="67" text-anchor="middle" fill="#ccc" font-size="16" font-weight="700">'+minsToHM(total)+'</text>';
    svgInner += '<text x="70" y="83" text-anchor="middle" fill="#888" font-size="10">\u603B\u65F6\u957F</text>';
  }
  svg.innerHTML = svgInner;

  const legendEl = document.getElementById(legendId);
  let legendHTML = '';
  stages.forEach(s => {
    if (s.val <= 0 && total === 0) return;
    const pct = total > 0 ? Math.round(s.val/total*100) : 0;
    legendHTML += '<div class="legend-item legend-'+s.key+'"><div class="legend-dot"></div><span>'+s.label+'</span><span class="legend-val">'+minsToHM(s.val)+' ('+pct+'%)</span></div>';
  });
  legendEl.innerHTML = legendHTML || '<span style="color:#555;">\u6682\u65E0\u6570\u636E</span>';
}

// ═══ 状态 ═══
const today = new Date();
const todayStr = fmtDate(today);
const allDates = __DATES_PLACEHOLDER__;
const firstDataDate = allDates[0] || todayStr;
const lastDataDate = allDates[allDates.length-1] || todayStr;
let viewMonday = new Date(getMonday(today));

// ═══ 渲染 ═══
function render() {
  const monday = viewMonday;
  const sunday = new Date(monday); sunday.setDate(sunday.getDate()+6);
  const currMonday = getMonday(today);
  const isCurrentWeek = monday.getTime() === currMonday.getTime();
  const days = getWeekDays(monday);

  document.getElementById("weekSub").textContent = 
    (isCurrentWeek ? "\u672C\u5468 " : "") + fmtMD(monday) + " - " + fmtMD(sunday);
  document.getElementById("weekLabel").textContent = fmtMD(monday) + " - " + fmtMD(sunday);

  const prevMonday = new Date(monday); prevMonday.setDate(prevMonday.getDate()-7);
  document.getElementById("btnPrev").disabled = prevMonday < getMonday(new Date(firstDataDate));
  document.getElementById("btnNext").disabled = isCurrentWeek;

  let cardioKcal=0, strengthKcal=0, totalTime=0, trainDays=0, displayDays=0;
  days.forEach(d => {
    const k = fmtDate(d);
    if (d > today) return;
    displayDays++;
    const data = TRAINING_DATA[k];
    if (!data) return;
    if ((data.cardio && data.cardio.length) || (data.strength && data.strength.length)) trainDays++;
    if (data.cardio) data.cardio.forEach(c => { cardioKcal+=c.kcal||0; totalTime+=c.time||0; });
    if (data.strength) data.strength.forEach(s => { strengthKcal+=s.kcal||0; totalTime+=s.time||0; });
  });

  document.getElementById("statsRow").innerHTML = `
    <div class="stat"><div class="stat-label">\u8BAD\u7EC3\u5929\u6570</div><div class="stat-value val-orange">\${trainDays}<span class="stat-unit">/\${displayDays}\u5929</span></div></div>
    <div class="stat"><div class="stat-label">\u6709\u6C27\u6D88\u8017</div><div class="stat-value val-green">\${cardioKcal}<span class="stat-unit">kcal</span></div></div>
    <div class="stat"><div class="stat-label">\u529B\u91CF\u6D88\u8017</div><div class="stat-value val-red">\${strengthKcal}<span class="stat-unit">kcal</span></div></div>
    <div class="stat"><div class="stat-label">\u603B\u65F6\u957F</div><div class="stat-value val-orange">\${Math.round(totalTime/60)}<span class="stat-unit">\u5206\u949F</span></div></div>
    <div class="stat"><div class="stat-label">\u603B\u6D88\u8017</div><div class="stat-value val-orange">\${cardioKcal+strengthKcal}<span class="stat-unit">kcal</span></div></div>`;

  let gridHTML = '';
  days.forEach(d => {
    const k = fmtDate(d);
    const data = TRAINING_DATA[k] || {cardio:null,strength:null};
    const isToday = k === todayStr;
    let cls = 'day';
    if (isToday) cls += ' today';
    const hasCardio = data.cardio && data.cardio.length;
    const hasStrength = data.strength && data.strength.length;
    if (hasCardio && hasStrength) cls += ' both';
    else if (hasCardio) cls += ' cardio';
    else if (hasStrength) cls += ' strength';
    else cls += ' empty';

    let tags='', detail='';
    if (hasCardio) { tags += '<span class="day-tag tag-cardio">\u6709\u6C27</span>'; }
    if (hasStrength) { tags += '<span class="day-tag tag-strength">\u529B\u91CF\u00D7'+data.strength.length+'</span>'; }
    if (!hasCardio && !hasStrength) {
      detail = isToday ? '\u4ECA\u5929' : (d > today ? '' : '\u4F11\u606F');
    }
    if (hasCardio && data.cardio[0]) {
      detail += '\01F6B4 '+secToHMS(data.cardio[0].time)+' \u00B7 '+data.cardio[0].kcal+'kcal';
      if (data.cardio[0].dist) detail += ' \u00B7 '+data.cardio[0].dist+'km';
    }
    if (hasStrength) {
      const totalStrKcal = data.strength.reduce((a,s)=>a+(s.kcal||0),0);
      const totalStrTime = data.strength.reduce((a,s)=>a+(s.time||0),0);
      detail += (detail?'<br>':'')+'\01F4AA '+secToHMS(totalStrTime)+' \u00B7 '+totalStrKcal+'kcal';
    }
    gridHTML += '<div class="'+cls+'"><div class="day-name">'+WEEKDAYS[d.getDay()===0?6:d.getDay()-1]+'</div><div class="day-date">'+fmtMD(d)+'</div>'+tags+'<div class="day-detail">'+detail+'</div></div>';
  });
  document.getElementById("weekGrid").innerHTML = gridHTML;

  renderTriplePanels(days);

  let detailHTML = '';
  days.forEach(d => {
    const k = fmtDate(d);
    const data = TRAINING_DATA[k];
    if (!data) return;
    const wd = WEEKDAYS[d.getDay()===0?6:d.getDay()-1];
    
    if (data.cardio) data.cardio.forEach(c => {
      const icon = c.type.includes('\u9A91\u884C')?'\01F6B4':c.type.includes('\u8DD1')?'\01F3C3':c.type.includes('\u722C')?'\01FA9C':'\01F3CB\0FE0F';
      detailHTML += '<div class="detail-card cardio-border"><h2>'+icon+' '+fmtMD(d)+' '+wd+' \u00B7 '+c.type+'</h2>'
        +'<div class="stat-row">'
        +'<div class="stat"><div class="stat-label">\u65F6\u957F</div><div class="stat-value val-green">'+secToHMS(c.time)+'</div></div>'
        +'<div class="stat"><div class="stat-label">\u6D88\u8017</div><div class="stat-value val-green">'+c.kcal+'<span class="stat-unit">kcal</span></div></div>'
        +(c.hr?'<div class="stat"><div class="stat-label">\u5FC3\u7387</div><div class="stat-value val-green">'+c.hr+'<span class="stat-unit">bpm</span></div></div>':'')
        +(c.dist?'<div class="stat"><div class="stat-label">\u8DDD\u79BB</div><div class="stat-value val-green">'+c.dist+'<span class="stat-unit">km</span></div></div>':'')
        +'</div></div>';
    });
    
    if (data.strength) data.strength.forEach((s, si) => {
      detailHTML += '<div class="detail-card strength-border"><h2>\01F4AA '+fmtMD(d)+' '+wd+' \u00B7 '+s.name+'</h2>'
        +'<p style="color:#888;font-size:13px;margin-bottom:16px;">\u603B\u65F6\u957F '+secToHMS(s.time)+' \u00B7 \u6D88\u8017 '+s.kcal+'kcal</p>';
      s.exercises.forEach((ex,i) => {
        detailHTML += '<div class="exercise-card"><div class="ex-name">'+(i+1)+'. '+ex.name+'</div><div class="sets-grid">';
        ex.sets.forEach(set => {
          if (set.wt && set.rep) detailHTML += '<div class="set-badge"><span class="wt">'+set.wt+'</span> &times; <span class="rp">'+set.rep+'</span></div>';
        });
        detailHTML += '</div></div>';
      });
      detailHTML += '</div>';
    });
  });
  document.getElementById("detailSection").innerHTML = detailHTML || '<div class="card empty-state"><p>\u672C\u5468\u6682\u65E0\u8BAD\u7EC3\u8BB0\u5F55</p></div>';

  let analysis = '';
  if (isCurrentWeek) {
    const plan = ["\u529B\u91CF\u65E5","\u6709\u6C27\u65E5","\u529B\u91CF\u65E5","\u6709\u6C27\u65E5","\u529B\u91CF\u65E5","\u6709\u6C27\u65E5","\u4F11\u606F"];
    analysis = '<strong>\01F4CB \u672C\u5468\u8BA1\u5212\u5BF9\u7167\uFF08\u4E00\u4E09\u4E94\u529B\u91CF / \u4E8C\u56DB\u516D\u6709\u6C27\uFF09\uFF1A</strong><br>';
    days.forEach((d,i) => {
      if (d > today) { analysis += '\u2B1C '+fmtMD(d)+' '+WEEKDAYS[i]+'\uFF1A'+plan[i]+'<br>'; return; }
      const k = fmtDate(d);
      const data = TRAINING_DATA[k];
      const hasCardio = data?.cardio?.length;
      const hasStrength = data?.strength?.length;
      const actual = hasCardio && hasStrength ? 'both' : hasCardio ? '\u6709\u6C27' : hasStrength ? '\u529B\u91CF('+data.strength.length+'\u573A)' : '\u672A\u7EC3';
      const icon = actual==='\u672A\u7EC3'?'\u274C':(plan[i].includes('\u529B\u91CF')&&actual.startsWith('\u6709\u6C27')||plan[i].includes('\u6709\u6C27')&&actual.startsWith('\u529B\u91CF'))?'\01F504':'\u2705';
      analysis += icon+' '+fmtMD(d)+' '+WEEKDAYS[i]+'\uFF1A'+actual+(actual==='\u672A\u7EC3'?'\uFF08\u7F3A\uFF09':'')+'<br>';
    });
    analysis += '<br><strong>\01F4A1 \u63D0\u793A\uFF1A</strong><br>'
      +'\u00B7 \u5168\u91CF\u6570\u636E\u8303\u56F4: '+firstDataDate+' \u2192 '+lastDataDate+'<br>'
      +'\u00B7 \u7EFF\u70B9\u7BA1\u7406\u7761\u7720\u8BB0\u5F55 \u00B7 \u9EC4\u70B9\u7BA1\u7406\u996E\u98DF\u8BB0\u5F55<br>'
      +'\u00B7 \u6BCF\u65E5\u81EA\u52A8\u7531 GitHub Actions \u66F4\u65B0';
  } else {
    analysis = '<strong>\01F4CA \u5386\u53F2\u5468\u56DE\u987E</strong><br>'
      +'\u00B7 \u8FD9\u662F '+fmtMD(monday)+' \u5230 '+fmtMD(sunday)+' \u7684\u8BAD\u7EC3\u6570\u636E<br>'
      +'\u00B7 \u4F7F\u7528 \u25C2 \u25B8 \u7BAD\u5934\u56DE\u5230\u672C\u5468';
  }
  document.getElementById("analysisBox").innerHTML = analysis;
}

function renderTriplePanels(days) {
  let trainHTML = '', sleepHTML = '', dietHTML = '';

  days.forEach(d => {
    const k = fmtDate(d);
    const wd = WEEKDAYS[d.getDay()===0?6:d.getDay()-1];
    const dateLabel = fmtMD(d) + ' ' + wd;
    const data = TRAINING_DATA[k];

    if (data && (data.cardio?.length || data.strength?.length)) {
      trainHTML += '<div class="panel-card train-panel"><div class="panel-date">'+dateLabel+'</div>';
      if (data.cardio) data.cardio.forEach(c => {
        trainHTML += '\01F6B4 '+c.type+' \u00B7 '+secToHMS(c.time)+' \u00B7 '+c.kcal+'kcal<br>';
      });
      if (data.strength) data.strength.forEach(s => {
        trainHTML += '\01F4AA '+s.name+' \u00B7 '+secToHMS(s.time)+' \u00B7 '+s.kcal+'kcal<br>';
      });
      trainHTML += '</div>';
    } else {
      trainHTML += '<div class="panel-card train-panel"><div class="panel-date">'+dateLabel+'</div><span style="color:#555;">\u4F11\u606F</span></div>';
    }

    const slp = SLEEP_DATA[k];
    if (slp && slp.bedTime && slp.wakeTime) {
      const dur = sleepDuration(slp.bedTime, slp.wakeTime);
      const awake = slp.awake || 0, rem = slp.rem || 0, light = slp.light || 0, deep = slp.deep || 0;
      const stageTotal = awake + rem + light + deep;
      let stageBar = '';
      if (stageTotal > 0) {
        const pcts = [awake/stageTotal, rem/stageTotal, light/stageTotal, deep/stageTotal];
        const colors = [STAGE_COLORS.awake, STAGE_COLORS.rem, STAGE_COLORS.light, STAGE_COLORS.deep];
        stageBar = '<div class="sleep-stages-mini">';
        pcts.forEach((p,i) => { if (p>0) stageBar += '<span style="flex:'+p+';background:'+colors[i]+';"></span>'; });
        stageBar += '</div>';
      }
      sleepHTML += '<div class="panel-card sleep-panel"><div class="panel-date">'+dateLabel+'</div>'
        +'<div class="sleep-info">'
        +'<div class="sleep-block"><div class="sleep-val">'+slp.bedTime+'</div><div class="sleep-lbl">\u5165\u7761</div></div>'
        +'<div class="sleep-divider">\u2192</div>'
        +'<div class="sleep-block"><div class="sleep-val">'+slp.wakeTime+'</div><div class="sleep-lbl">\u8D77\u5E8A</div></div>'
        +'</div>'
        +'<div style="margin-top:4px;"><span class="sleep-duration">'+dur+'</span>'
        +(stageTotal>0?' \u00B7 \u6DF1\u7761'+minsToHM(deep):'')
        +'</div>'
        +stageBar
        +'</div>';
    } else {
      sleepHTML += '<div class="panel-card sleep-panel"><div class="panel-date">'+dateLabel+'</div><span style="color:#555;">\u65E0\u8BB0\u5F55</span></div>';
    }

    const diet = DIET_DATA[k];
    if (diet && diet.meals && diet.meals.length > 0) {
      dietHTML += '<div class="panel-card diet-panel"><div class="panel-date">'+dateLabel+'</div>';
      dietHTML += '<span style="color:#ff6b35;font-weight:600;">'+diet.totalKcal+' kcal</span> \u00B7 ';
      dietHTML += '<span style="color:#ce93d8;">\u86CB\u767D'+diet.protein+'g</span> \u00B7 ';
      dietHTML += '<span style="color:#fff176;">\u8102\u80AA'+diet.fat+'g</span> \u00B7 ';
      dietHTML += '<span style="color:#80cbc4;">\u78B3\u6C34'+diet.carb+'g</span>';
      dietHTML += '<div style="font-size:11px;color:#666;margin-top:4px;">'+diet.meals.length+'\u9910</div>';
      dietHTML += '</div>';
    } else {
      dietHTML += '<div class="panel-card diet-panel"><div class="panel-date">'+dateLabel+'</div><span style="color:#555;">\u65E0\u8BB0\u5F55</span></div>';
    }
  });

  document.getElementById("trainCol").innerHTML = trainHTML || '<div class="empty-state" style="padding:20px;"><p>\u6682\u65E0\u6570\u636E</p></div>';
  document.getElementById("sleepCol").innerHTML = sleepHTML || '<div class="empty-state" style="padding:20px;"><p>\u6682\u65E0\u6570\u636E</p></div>';
  document.getElementById("dietCol").innerHTML = dietHTML || '<div class="empty-state" style="padding:20px;"><p>\u6682\u65E0\u6570\u636E</p></div>';
}

function prevWeek() { viewMonday.setDate(viewMonday.getDate()-7); render(); }
function nextWeek() {
  const next = new Date(viewMonday);
  next.setDate(next.getDate()+7);
  if (next <= getMonday(today)) { viewMonday = next; render(); }
}

function hideCards() {
  document.getElementById("overviewCard").classList.add("collapsed");
  document.getElementById("weekViewCard").classList.add("collapsed");
  document.getElementById("floatingRed").classList.add("show");
}
function restoreCards() {
  document.getElementById("overviewCard").classList.remove("collapsed");
  document.getElementById("weekViewCard").classList.remove("collapsed");
  document.getElementById("floatingRed").classList.remove("show");
}

// ═══ 睡眠弹窗 ═══
let sleepAuthed = false;
let editingSleepDate = null;

function openSleep() {
  document.getElementById("sleepModal").classList.add("show");
  if (sleepAuthed) {
    showSleepEditor();
  } else {
    document.getElementById("sleepPwdBox").style.display = 'block';
    document.getElementById("sleepEditForm").classList.remove("show");
    document.getElementById("sleepPwdInput").value = '';
    document.getElementById("sleepPwdErr").textContent = '';
  }
}
function closeSleep() {
  document.getElementById("sleepModal").classList.remove("show");
  sleepAuthed = false;
  editingSleepDate = null;
  document.getElementById("sleepPwdBox").style.display = 'block';
  document.getElementById("sleepEditForm").classList.remove("show");
  document.getElementById("sleepAddForm").style.display = 'none';
}
function verifySleepPwd() {
  if (document.getElementById("sleepPwdInput").value === AUTH_PASSWORD) {
    sleepAuthed = true;
    document.getElementById("sleepPwdBox").style.display = 'none';
    showSleepEditor();
  } else {
    document.getElementById("sleepPwdErr").textContent = '\u5BC6\u7801\u9519\u8BEF';
  }
}
function showSleepEditor() {
  document.getElementById("sleepEditForm").classList.add("show");
  const dp = document.getElementById("sleepDatePicker");
  dp.value = editingSleepDate || todayStr;
  renderSleepRecords();
}

function onSleepDateChange() {
  editingSleepDate = document.getElementById("sleepDatePicker").value;
  renderSleepRecords();
  document.getElementById("sleepAddForm").style.display = 'none';
  document.getElementById("btnAddSleep").style.display = 'block';
}

function renderSleepRecords() {
  const dateKey = editingSleepDate || todayStr;
  const s = SLEEP_DATA[dateKey];
  let html = '';
  if (s && s.bedTime && s.wakeTime) {
    const awake = s.awake || 0, rem = s.rem || 0, light = s.light || 0, deep = s.deep || 0;
    html += '<div class="sleep-record-card">'
      +'<div class="sr-info">'
      +'<div class="sr-date">'+dateKey+'</div>'
      +'<div class="sr-detail">\u5165\u7761 '+s.bedTime+' \u2192 \u8D77\u5E8A '+s.wakeTime+' \u00B7 '+sleepDuration(s.bedTime,s.wakeTime)+'</div>'
      +'<div class="sr-stages">'
      +(awake?'<span class="stage-tag-awake">\u6E05\u9192 '+minsToHM(awake)+'</span>':'')
      +(rem?'<span class="stage-tag-rem">REM '+minsToHM(rem)+'</span>':'')
      +(light?'<span class="stage-tag-light">\u6D45\u7761 '+minsToHM(light)+'</span>':'')
      +(deep?'<span class="stage-tag-deep">\u6DF1\u7761 '+minsToHM(deep)+'</span>':'')
      +'</div>'
      +(s.note?'<div style="font-size:11px;color:#666;margin-top:2px;">'+s.note+'</div>':'')
      +'</div>'
      +'<div class="sr-actions">'
      +'<button class="sr-edit" onclick="editSleep(\''+dateKey+'\')">\u7F16\u8F91</button>'
      +'<button class="sr-del" onclick="deleteSleep(\''+dateKey+'\')">\u5220\u9664</button>'
      +'</div></div>';
    document.getElementById("btnAddSleep").style.display = 'none';
  } else {
    html = '<div class="empty-state" style="padding:24px;"><p>'+dateKey+' \u6682\u65E0\u7761\u7720\u8BB0\u5F55</p></div>';
    document.getElementById("btnAddSleep").style.display = 'block';
  }
  document.getElementById("sleepRecordsList").innerHTML = html;
}

function showSleepAddForm(dateOverride) {
  editingSleepDate = dateOverride || document.getElementById("sleepDatePicker").value || todayStr;
  document.getElementById("sleepDatePicker").value = editingSleepDate;
  document.getElementById("sleepAddForm").style.display = 'block';
  document.getElementById("btnAddSleep").style.display = 'none';
  document.getElementById("sBedTime").value = '';
  document.getElementById("sWakeTime").value = '';
  document.getElementById("sAwake").value = '';
  document.getElementById("sRem").value = '';
  document.getElementById("sLight").value = '';
  document.getElementById("sDeep").value = '';
  document.getElementById("sNote").value = '';

  if (dateOverride && SLEEP_DATA[dateOverride]) {
    const s = SLEEP_DATA[dateOverride];
    document.getElementById("sBedTime").value = s.bedTime || '';
    document.getElementById("sWakeTime").value = s.wakeTime || '';
    document.getElementById("sAwake").value = s.awake || '';
    document.getElementById("sRem").value = s.rem || '';
    document.getElementById("sLight").value = s.light || '';
    document.getElementById("sDeep").value = s.deep || '';
    document.getElementById("sNote").value = s.note || '';
  }
  updateSleepPie();
}

function cancelSleepAdd() {
  document.getElementById("sleepAddForm").style.display = 'none';
  document.getElementById("btnAddSleep").style.display = 'block';
  editingSleepDate = null;
}

function updateSleepPie() {
  const awake = parseInt(document.getElementById("sAwake").value) || 0;
  const rem = parseInt(document.getElementById("sRem").value) || 0;
  const light = parseInt(document.getElementById("sLight").value) || 0;
  const deep = parseInt(document.getElementById("sDeep").value) || 0;
  drawPieChart('sleepPieSvg', 'sleepPieLegend', 'sleepTotalLabel', awake, rem, light, deep);
}

function saveSleepRecord() {
  const dateKey = editingSleepDate || document.getElementById("sleepDatePicker").value || todayStr;
  const bedTime = document.getElementById("sBedTime").value;
  const wakeTime = document.getElementById("sWakeTime").value;
  const awake = parseInt(document.getElementById("sAwake").value) || 0;
  const rem = parseInt(document.getElementById("sRem").value) || 0;
  const light = parseInt(document.getElementById("sLight").value) || 0;
  const deep = parseInt(document.getElementById("sDeep").value) || 0;
  const note = document.getElementById("sNote").value;
  if (!bedTime || !wakeTime) { alert('\u8BF7\u586B\u5199\u5165\u7761\u548C\u8D77\u5E8A\u65F6\u95F4'); return; }
  SLEEP_DATA[dateKey] = { bedTime, wakeTime, awake, rem, light, deep, note };
  saveSleepData();
  cancelSleepAdd();
  renderSleepRecords();
  render();
}
function editSleep(dateKey) { showSleepAddForm(dateKey); }
function deleteSleep(dateKey) {
  if (confirm('\u5220\u9664 '+dateKey+' \u7684\u7761\u7720\u8BB0\u5F55\uFF1F')) {
    delete SLEEP_DATA[dateKey];
    saveSleepData();
    renderSleepRecords();
    render();
  }
}
document.getElementById("sleepModal").addEventListener("click",function(e){ if(e.target===this)closeSleep(); });

// ═══ 饮食弹窗 ═══
let dietAuthed = false;
let editingDietDate = null;

function openDiet() {
  editingDietDate = null;
  document.getElementById("dietModal").classList.add("show");
  if (dietAuthed) {
    showDietEditor();
  } else {
    document.getElementById("dietPwdBox").style.display = 'block';
    document.getElementById("dietEditForm").classList.remove("show");
    document.getElementById("dietPwdInput").value = '';
    document.getElementById("dietPwdErr").textContent = '';
  }
}
function closeDiet() {
  document.getElementById("dietModal").classList.remove("show");
  dietAuthed = false;
  editingDietDate = null;
  document.getElementById("dietPwdBox").style.display = 'block';
  document.getElementById("dietEditForm").classList.remove("show");
  document.getElementById("dietAddForm").style.display = 'none';
}
function verifyDietPwd() {
  if (document.getElementById("dietPwdInput").value === AUTH_PASSWORD) {
    dietAuthed = true;
    document.getElementById("dietPwdBox").style.display = 'none';
    showDietEditor();
  } else {
    document.getElementById("dietPwdErr").textContent = '\u5BC6\u7801\u9519\u8BEF';
  }
}
function showDietEditor() {
  document.getElementById("dietEditForm").classList.add("show");
  document.getElementById("dietDatePicker").value = editingDietDate || todayStr;
  renderDietContent();
}

function onDietDateChange() {
  editingDietDate = document.getElementById("dietDatePicker").value;
  renderDietContent();
  document.getElementById("dietAddForm").style.display = 'none';
  document.getElementById("btnAddDiet").style.display = 'block';
}

function renderDietContent() {
  const dateKey = editingDietDate || document.getElementById("dietDatePicker").value || todayStr;
  const data = DIET_DATA[dateKey];
  let summaryHTML = '';
  if (data && data.meals && data.meals.length > 0) {
    summaryHTML += '<div class="diet-summary">'
      +'<div class="diet-stat"><div class="label">\u603B\u6444\u5165</div><div class="value" style="color:#ff6b35">'+(data.totalKcal||0)+'<span style="font-size:12px;color:#888"> kcal</span></div></div>'
      +'<div class="diet-stat"><div class="label">\u86CB\u767D\u8D28</div><div class="value" style="color:#ce93d8">'+(data.protein||0)+'<span style="font-size:12px;color:#888"> g</span></div></div>'
      +'<div class="diet-stat"><div class="label">\u8102\u80AA</div><div class="value" style="color:#fff176">'+(data.fat||0)+'<span style="font-size:12px;color:#888"> g</span></div></div>'
      +'<div class="diet-stat"><div class="label">\u78B3\u6C34</div><div class="value" style="color:#80cbc4">'+(data.carb||0)+'<span style="font-size:12px;color:#888"> g</span></div></div>'
      +'</div>';
  } else {
    summaryHTML = '<div class="empty-state" style="padding:16px;"><p>'+dateKey+' \u6682\u65E0\u996E\u98DF\u8BB0\u5F55</p></div>';
  }
  document.getElementById("dietDaySummary").innerHTML = summaryHTML;

  let mealsHTML = '';
  if (data && data.meals) {
    data.meals.forEach((m,i) => {
      mealsHTML += '<div class="meal-card">'
        +'<button class="del-meal" onclick="deleteDietMeal('+i+')" title="\u5220\u9664\u6B64\u9910">\u2715</button>'
        +'<div class="meal-header"><span class="meal-name">'+m.name+'</span><span class="meal-kcal">'+m.kcal+' kcal</span></div>'
        +'<div class="meal-items">'+(m.items||[]).join('\u3001')+'</div>'
        +'<div class="macro-row">'+(m.protein?'<span class="macro-tag macro-protein">\u86CB\u767D '+m.protein+'g</span>':'')+(m.fat?'<span class="macro-tag macro-fat">\u8102\u80AA '+m.fat+'g</span>':'')+(m.carb?'<span class="macro-tag macro-carb">\u78B3\u6C34 '+m.carb+'g</span>':'')+'</div></div>';
    });
  }
  document.getElementById("dietMealsList").innerHTML = mealsHTML;
  document.getElementById("btnAddDiet").style.display = 'block';
}

function showDietAddForm() {
  document.getElementById("dietAddForm").style.display = 'block';
  document.getElementById("btnAddDiet").style.display = 'none';
  document.getElementById("dMealName").value = '';
  document.getElementById("dItems").value = '';
  document.getElementById("dKcal").value = '';
  document.getElementById("dProtein").value = '';
  document.getElementById("dFat").value = '';
  document.getElementById("dCarb").value = '';
}
function cancelDietAdd() {
  document.getElementById("dietAddForm").style.display = 'none';
  document.getElementById("btnAddDiet").style.display = 'block';
}
function saveDietMeal() {
  const dateKey = editingDietDate || document.getElementById("dietDatePicker").value || todayStr;
  const name = document.getElementById("dMealName").value.trim();
  const itemsStr = document.getElementById("dItems").value.trim();
  const kcal = parseInt(document.getElementById("dKcal").value) || 0;
  const protein = parseFloat(document.getElementById("dProtein").value) || 0;
  const fat = parseFloat(document.getElementById("dFat").value) || 0;
  const carb = parseFloat(document.getElementById("dCarb").value) || 0;
  if (!name) { alert('\u8BF7\u586B\u5199\u9910\u6B21\u540D\u79F0'); return; }
  const items = itemsStr ? itemsStr.split(/[,,\uFF0C]/).map(s=>s.trim()).filter(Boolean) : [];
  const meal = { name, items, kcal, protein, fat, carb };

  if (!DIET_DATA[dateKey]) DIET_DATA[dateKey] = { meals: [], totalKcal: 0, protein: 0, fat: 0, carb: 0 };
  DIET_DATA[dateKey].meals.push(meal);
  recalcDietTotals(dateKey);
  saveDietData();
  cancelDietAdd();
  renderDietContent();
  render();
}
function deleteDietMeal(idx) {
  const dateKey = editingDietDate || document.getElementById("dietDatePicker").value || todayStr;
  if (!DIET_DATA[dateKey]) return;
  if (confirm('\u5220\u9664\u8FD9\u4E00\u9910\uFF1F')) {
    DIET_DATA[dateKey].meals.splice(idx, 1);
    recalcDietTotals(dateKey);
    saveDietData();
    renderDietContent();
    render();
  }
}
function recalcDietTotals(dateKey) {
  const data = DIET_DATA[dateKey];
  if (!data || !data.meals) return;
  let totalKcal = 0, protein = 0, fat = 0, carb = 0;
  data.meals.forEach(m => {
    totalKcal += m.kcal || 0;
    protein += m.protein || 0;
    fat += m.fat || 0;
    carb += m.carb || 0;
  });
  data.totalKcal = Math.round(totalKcal);
  data.protein = Math.round(protein*10)/10;
  data.fat = Math.round(fat*10)/10;
  data.carb = Math.round(carb*10)/10;
}
document.getElementById("dietModal").addEventListener("click",function(e){ if(e.target===this)closeDiet(); });

// ═══ 启动 ═══
loadLocalData();
render();
</script>

<p style="color:#555;font-size:12px;margin-top:16px;text-align:center;">
  数据来自训记 API · 绿点管理睡眠 · 黄点管理饮食 · 由 GitHub Actions 每日自动更新<br>
  数据范围: __FIRST_DATE__ → __LAST_DATE__
</p>
</body>
</html>"""

# ═══════════════════════════════════════════
# 使用安全的 .replace() 拼装最终 HTML
# ═══════════════════════════════════════════

output_html = html_template.replace("__JS_DATA_PLACEHOLDER__", js_data_string)
output_html = output_html.replace("__DATES_PLACEHOLDER__", json.dumps(dates))
output_html = output_html.replace("__FIRST_DATE__", first_date)
output_html = output_html.replace("__LAST_DATE__", last_date)

with open("train_report.html", "w", encoding="utf-8") as f:
    f.write(output_html)

print(f"\nOK - train_report.html 已生成 ({len(dates)} 天)")
