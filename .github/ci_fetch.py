#!/usr/bin/env python3
"""
CI 版：从环境变量读取 API Key，拉取全量训练数据并生成 train_report.html
用于 GitHub Actions 每日定时运行
"""
import json, os, re, ssl, sys
import urllib.request, gzip, time
from datetime import date, timedelta

API_KEY = os.environ.get("XUNJI_API_KEY", "")
if not API_KEY:
    print("ERROR: XUNJI_API_KEY 环境变量未设置")
    sys.exit(1)

BASE_URL = "https://trains.xunjiapp.cn"
today = date.today()
start = date(2026, 4, 1)
days_count = (today - start).days + 1

print(f"从 {start} 到 {today}，共 {days_count} 天，拉取中...")
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
# 解析器（与 gen_full_report.py v5 一致）
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
        elif re.match(r"^\d+\.", p):
            result["type"] = re.sub(r"^\d+\.", "", p)
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
            elif not re.match(r"^\d+\.", p) and p != "":
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
        if re.match(r"^\d+\.", p):
            if current_ex and current_sets:
                exercises.append({"name": current_ex, "sets": current_sets})
            current_ex = re.sub(r"^\d+\.", "", p)
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
total_strength = sum(len(d["strength"]) for d in parsed_data.values())
total_cardio = sum(len(d["cardio"]) for d in parsed_data.values())
first_date = dates[0]
last_date = dates[-1]

print(f"\n--- 汇总 ---")
print(f"总训练天数: {len(dates)}")
print(f"力量: {total_strength} | 有氧: {total_cardio}")
print(f"数据范围: {first_date} → {last_date}")


# ═══════════════════════════════════════════
# 生成 JS 数据
# ═══════════════════════════════════════════

def escape_js(s):
    return s.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"').replace("\n", "\\n")


js_lines = ["const TRAINING_DATA = {"]
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

js_lines.append("};")
js_data = "\n".join(js_lines)


# ═══════════════════════════════════════════
# 生成 HTML（与 gen_full_report.py v5 一致）
# ═══════════════════════════════════════════

with open("train_report.html", "w", encoding="utf-8") as f:
    f.write(
        f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>训记训练数据 · 全量汇总</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#0f0f0f; color:#e0e0e0; padding:24px; max-width:860px; margin:0 auto; }}
h1 {{ font-size:24px; margin-bottom:4px; }}
.sub {{ color:#888; font-size:14px; margin-bottom:24px; transition:color .3s; }}

.floating-red {{
  position:fixed; top:24px; right:24px; z-index:200;
  width:36px; height:36px; border-radius:50%; background:#ff5f57;
  border:none; cursor:pointer; opacity:0; transform:scale(0); pointer-events:none;
  transition: opacity .4s, transform .4s cubic-bezier(.34,1.56,.64,1);
  box-shadow: 0 0 18px rgba(255,95,87,.4);
}}
.floating-red.show {{ opacity:1; transform:scale(1); pointer-events:auto; }}
.floating-red:hover {{ transform:scale(1.2); box-shadow: 0 0 24px rgba(255,95,87,.6); }}
.floating-red::after {{ content:'\\1F534'; font-size:14px; position:absolute; inset:0; display:flex; align-items:center; justify-content:center; }}

.card {{ background:#1a1a1a; border-radius:12px; padding:20px; margin-bottom:16px; border:1px solid #2a2a2a; position:relative; transition: opacity .5s, transform .5s, max-height .5s, padding .5s, margin .5s; overflow:hidden; }}
.card.collapsed {{ opacity:0; transform:translateY(-20px); max-height:0; padding-top:0; padding-bottom:0; margin-bottom:0; border-width:0; }}

.card-header {{ display:flex; align-items:center; justify-content:space-between; margin-bottom:14px; }}
.card-header h2 {{ font-size:16px; color:#aaa; margin:0; }}
.header-right {{ display:flex; align-items:center; gap:10px; }}

.week-nav {{ display:flex; align-items:center; gap:6px; }}
.week-nav button {{ background:#2a2a2a; border:none; color:#aaa; width:26px;height:26px; border-radius:6px; cursor:pointer; font-size:14px; display:flex; align-items:center; justify-content:center; transition:all .2s; }}
.week-nav button:hover:not(:disabled) {{ background:#3a3a3a; color:#fff; }}
.week-nav button:disabled {{ opacity:.3; cursor:default; }}
.week-label {{ font-size:12px; color:#888; min-width:100px; text-align:center; user-select:none; }}

.traffic-lights {{ display:flex; gap:8px; align-items:center; }}
.traffic-dot {{ width:13px; height:13px; border-radius:50%; cursor:pointer; transition:all .25s; position:relative; }}
.traffic-dot::after {{ content:''; position:absolute; inset:0; border-radius:50%; opacity:0; transition:opacity .2s; }}
.traffic-dot:hover::after {{ opacity:1; }}
.traffic-dot.red {{ background:#ff5f57; }}
.traffic-dot.red::after {{ box-shadow:0 0 10px rgba(255,95,87,.5); }}
.traffic-dot.yellow {{ background:#febc2e; }}
.traffic-dot.yellow::after {{ box-shadow:0 0 10px rgba(254,188,46,.5); }}
.traffic-dot.green {{ background:#28c840; }}
.traffic-dot.green::after {{ box-shadow:0 0 10px rgba(40,200,64,.5); }}
.traffic-dot:hover {{ transform:scale(1.15); }}

.stat-row {{ display:flex; gap:16px; flex-wrap:wrap; }}
.stat {{ background:#222; border-radius:8px; padding:16px; flex:1; min-width:110px; text-align:center; transition:all .4s; }}
.stat-label {{ font-size:12px; color:#888; margin-bottom:4px; }}
.stat-value {{ font-size:28px; font-weight:700; transition:all .4s; }}
.stat-unit {{ font-size:14px; color:#888; margin-left:2px; }}
.val-red {{ color:#f44336; }}
.val-green {{ color:#4caf50; }}
.val-orange {{ color:#ff6b35; }}

.week-grid {{ display:grid; grid-template-columns:repeat(7,1fr); gap:8px; }}
.day {{ background:#222; border-radius:8px; padding:12px 8px; text-align:center; min-height:90px; transition:all .4s; cursor:default; }}
.day.today {{ border:2px solid #ff6b35; }}
.day.cardio {{ background:#1a2a1a; border:1px solid #2d5a2d; }}
.day.strength {{ background:#2a1a1a; border:1px solid #5a2d2d; }}
.day.both {{ background:linear-gradient(135deg, #1a2a1a 50%, #2a1a1a 50%); border:1px solid #444; }}
.day.empty {{ color:#555; }}
.day-name {{ font-size:12px; color:#888; margin-bottom:4px; }}
.day-date {{ font-size:11px; color:#666; margin-bottom:6px; }}
.day-tag {{ font-size:10px; padding:2px 6px; border-radius:4px; display:inline-block; margin:2px; }}
.tag-cardio {{ background:#1a3a1a; color:#4caf50; }}
.tag-strength {{ background:#3a1a1a; color:#f44336; }}
.day-detail {{ font-size:11px; color:#ccc; line-height:1.5; margin-top:4px; }}

.exercise-card {{ background:#222; border-radius:8px; padding:16px; margin-bottom:10px; border-left:3px solid #f44336; }}
.ex-name {{ font-size:15px; font-weight:600; margin-bottom:6px; color:#f44336; }}
.sets-grid {{ display:flex; flex-wrap:wrap; gap:6px; }}
.set-badge {{ background:#2a2a2a; border-radius:6px; padding:6px 10px; font-size:12px; color:#ccc; }}
.set-badge .wt {{ color:#ff9800; font-weight:600; }}
.set-badge .rp {{ color:#4caf50; }}

.note {{ background:#1a1a2a; border:1px solid #2a2a3a; border-radius:8px; padding:14px; margin-top:12px; font-size:13px; color:#888; line-height:1.8; }}
.note strong {{ color:#ff6b35; }}
.warn {{ background:#2a1a1a; border:1px solid #5a2d2d; border-radius:8px; padding:12px; font-size:13px; color:#f44336; line-height:1.8; }}
.empty-state {{ text-align:center; padding:40px 20px; color:#555; }}
.empty-state .icon {{ font-size:48px; margin-bottom:12px; }}
.detail-card {{ background:#1a1a1a; border-radius:12px; padding:20px; margin-bottom:16px; border:1px solid #2a2a2a; }}
.detail-card.cardio-border {{ border-left:3px solid #4caf50; }}
.detail-card.strength-border {{ border-left:3px solid #f44336; }}

.modal-overlay {{ position:fixed; inset:0; background:rgba(0,0,0,.7); z-index:100; display:flex; align-items:center; justify-content:center; opacity:0; pointer-events:none; transition:opacity .3s; }}
.modal-overlay.show {{ opacity:1; pointer-events:auto; }}
.modal {{ background:#1a1a1a; border:1px solid #2a2a2a; border-radius:12px; width:560px; max-height:80vh; overflow-y:auto; padding:24px; position:relative; }}
.modal h2 {{ font-size:18px; color:#febc2e; margin-bottom:16px; }}
.modal-close {{ position:absolute; top:12px; right:16px; background:none; border:none; color:#888; font-size:20px; cursor:pointer; }}
.modal-close:hover {{ color:#fff; }}

.meal-card {{ background:#222; border-radius:8px; padding:14px; margin-bottom:10px; border-left:3px solid #febc2e; }}
.meal-header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }}
.meal-name {{ font-size:14px; font-weight:600; color:#febc2e; }}
.meal-kcal {{ font-size:13px; color:#ff6b35; font-weight:600; }}
.meal-items {{ font-size:12px; color:#aaa; line-height:1.7; }}
.macro-row {{ display:flex; gap:12px; margin-top:8px; font-size:11px; }}
.macro-tag {{ padding:2px 8px; border-radius:4px; }}
.macro-protein {{ background:#2a1a2a; color:#ce93d8; }}
.macro-fat {{ background:#2a2a1a; color:#fff176; }}
.macro-carb {{ background:#1a2a2a; color:#80cbc4; }}
.diet-summary {{ display:flex; gap:12px; margin-bottom:20px; }}
.diet-stat {{ flex:1; background:#222; border-radius:8px; padding:12px; text-align:center; }}
.diet-stat .label {{ font-size:11px; color:#888; }}
.diet-stat .value {{ font-size:20px; font-weight:700; margin-top:4px; }}

.strength-group {{ margin-bottom:16px; }}
.strength-group-title {{ font-size:13px; color:#f44336; font-weight:600; margin-bottom:8px; padding:4px 0; border-bottom:1px solid #333; }}
</style>
</head>
<body>

<h1>🏋️ 训记训练数据</h1>
<p class="sub" id="weekSub"></p>

<button class="floating-red" id="floatingRed" onclick="restoreCards()" title="恢复面板"></button>

<div class="card" id="overviewCard">
  <div class="card-header">
    <h2>📊 本周概览</h2>
    <div class="header-right">
      <div class="week-nav">
        <button onclick="prevWeek()" id="btnPrev" title="上一周">◂</button>
        <span class="week-label" id="weekLabel"></span>
        <button onclick="nextWeek()" id="btnNext" title="下一周">▸</button>
      </div>
      <div class="traffic-lights">
        <div class="traffic-dot red" title="隐藏面板" onclick="hideCards()"></div>
        <div class="traffic-dot yellow" title="饮食数据" onclick="openDiet()"></div>
        <div class="traffic-dot green" title="展开全部" onclick="toggleExpand()"></div>
      </div>
    </div>
  </div>
  <div class="stat-row" id="statsRow"></div>
</div>

<div class="card" id="weekViewCard">
  <div class="card-header"><h2>📅 周视图</h2></div>
  <div class="week-grid" id="weekGrid"></div>
</div>

<div id="detailSection"></div>
<div class="warn" id="analysisBox"></div>

<div class="modal-overlay" id="dietModal">
  <div class="modal">
    <button class="modal-close" onclick="closeDiet()">✕</button>
    <h2>🍽️ 饮食数据</h2>
    <div id="dietContent">
      <div class="empty-state">
        <div class="icon">🥗</div>
        <p>还没有饮食数据</p>
        <p style="font-size:12px;margin-top:4px;">导出 DeepSeek 聊天记录后同步到这里</p>
      </div>
    </div>
  </div>
</div>

<script>
// ═══ 全量训练数据 ═══
{js_data}

// ═══ 饮食数据（待同步） ═══
const DIET_DATA = {{}};

// ═══ 工具函数（v5: 使用本地日期方法） ═══
function fmtDate(d) {{
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return y + '-' + m + '-' + day;
}}
function getMonday(d) {{
  const m = new Date(d);
  const day = m.getDay();
  const diff = day === 0 ? -6 : 1 - day;
  m.setDate(m.getDate() + diff);
  m.setHours(0,0,0,0);
  return m;
}}
function fmtMD(d) {{ return (d.getMonth()+1)+"/"+d.getDate(); }}
const WEEKDAYS = ["周一","周二","周三","周四","周五","周六","周日"];
function getWeekDays(monday) {{
  const days = [];
  for (let i=0;i<7;i++) {{ const d=new Date(monday); d.setDate(d.getDate()+i); days.push(d); }}
  return days;
}}
function secToHMS(s) {{
  if (!s) return "0:00";
  const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;
  return h>0?h+":"+String(m).padStart(2,'0')+":"+String(sec).padStart(2,'0'):m+":"+String(sec).padStart(2,'0');
}}

// ═══ 状态 ═══
const today = new Date();
const todayStr = fmtDate(today);
const allDates = Object.keys(TRAINING_DATA).sort();
const firstDataDate = allDates[0];
const lastDataDate = allDates[allDates.length-1];
let viewMonday = new Date(getMonday(today));

// ═══ 渲染 ═══
function render() {{
  const monday = viewMonday;
  const sunday = new Date(monday); sunday.setDate(sunday.getDate()+6);
  const currMonday = getMonday(today);
  const isCurrentWeek = monday.getTime() === currMonday.getTime();
  const days = getWeekDays(monday);

  document.getElementById("weekSub").textContent = 
    (isCurrentWeek ? "本周 " : "") + fmtMD(monday) + " - " + fmtMD(sunday);
  document.getElementById("weekLabel").textContent = fmtMD(monday) + " - " + fmtMD(sunday);

  const prevMonday = new Date(monday); prevMonday.setDate(prevMonday.getDate()-7);
  document.getElementById("btnPrev").disabled = prevMonday < getMonday(new Date(firstDataDate));
  document.getElementById("btnNext").disabled = isCurrentWeek;

  let cardioKcal=0, strengthKcal=0, totalTime=0, trainDays=0, displayDays=0;
  days.forEach(d => {{
    const k = fmtDate(d);
    if (d > today) return;
    displayDays++;
    const data = TRAINING_DATA[k];
    if (!data) return;
    if ((data.cardio && data.cardio.length) || (data.strength && data.strength.length)) trainDays++;
    if (data.cardio) data.cardio.forEach(c => {{ cardioKcal+=c.kcal||0; totalTime+=c.time||0; }});
    if (data.strength) data.strength.forEach(s => {{ strengthKcal+=s.kcal||0; totalTime+=s.time||0; }});
  }});

  document.getElementById("statsRow").innerHTML = `
    <div class="stat"><div class="stat-label">训练天数</div><div class="stat-value val-orange">${{trainDays}}<span class="stat-unit">/${{displayDays}}天</span></div></div>
    <div class="stat"><div class="stat-label">有氧消耗</div><div class="stat-value val-green">${{cardioKcal}}<span class="stat-unit">kcal</span></div></div>
    <div class="stat"><div class="stat-label">力量消耗</div><div class="stat-value val-red">${{strengthKcal}}<span class="stat-unit">kcal</span></div></div>
    <div class="stat"><div class="stat-label">总时长</div><div class="stat-value val-orange">${{Math.round(totalTime/60)}}<span class="stat-unit">分钟</span></div></div>
    <div class="stat"><div class="stat-label">总消耗</div><div class="stat-value val-orange">${{cardioKcal+strengthKcal}}<span class="stat-unit">kcal</span></div></div>`;

  let gridHTML = '';
  days.forEach(d => {{
    const k = fmtDate(d);
    const data = TRAINING_DATA[k] || {{cardio:null,strength:null}};
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
    if (hasCardio) {{
      tags += '<span class="day-tag tag-cardio">有氧</span>';
    }}
    if (hasStrength) {{
      tags += '<span class="day-tag tag-strength">力量×'+data.strength.length+'</span>';
    }}
    if (!hasCardio && !hasStrength) {{
      detail = isToday ? '今天' : (d > today ? '' : '休息');
    }}
    if (hasCardio && data.cardio[0]) {{
      detail += '🚴 '+secToHMS(data.cardio[0].time)+' · '+data.cardio[0].kcal+'kcal';
      if (data.cardio[0].dist) detail += ' · '+data.cardio[0].dist+'km';
    }}
    if (hasStrength) {{
      const totalStrKcal = data.strength.reduce((a,s)=>a+(s.kcal||0),0);
      const totalStrTime = data.strength.reduce((a,s)=>a+(s.time||0),0);
      detail += (detail?'<br>':'')+'💪 '+secToHMS(totalStrTime)+' · '+totalStrKcal+'kcal';
    }}
    gridHTML += '<div class="'+cls+'"><div class="day-name">'+WEEKDAYS[d.getDay()===0?6:d.getDay()-1]+'</div><div class="day-date">'+fmtMD(d)+'</div>'+tags+'<div class="day-detail">'+detail+'</div></div>';
  }});
  document.getElementById("weekGrid").innerHTML = gridHTML;

  let detailHTML = '';
  days.forEach(d => {{
    const k = fmtDate(d);
    const data = TRAINING_DATA[k];
    if (!data) return;
    const wd = WEEKDAYS[d.getDay()===0?6:d.getDay()-1];
    
    if (data.cardio) data.cardio.forEach(c => {{
      const icon = c.type.includes('骑行')?'🚴':c.type.includes('跑')?'🏃':c.type.includes('爬')?'🪜':'🏋️';
      detailHTML += '<div class="detail-card cardio-border"><h2>'+icon+' '+fmtMD(d)+' '+wd+' · '+c.type+'</h2>'
        +'<div class="stat-row">'
        +'<div class="stat"><div class="stat-label">时长</div><div class="stat-value val-green">'+secToHMS(c.time)+'</div></div>'
        +'<div class="stat"><div class="stat-label">消耗</div><div class="stat-value val-green">'+c.kcal+'<span class="stat-unit">kcal</span></div></div>'
        +(c.hr?'<div class="stat"><div class="stat-label">心率</div><div class="stat-value val-green">'+c.hr+'<span class="stat-unit">bpm</span></div></div>':'')
        +(c.dist?'<div class="stat"><div class="stat-label">距离</div><div class="stat-value val-green">'+c.dist+'<span class="stat-unit">km</span></div></div>':'')
        +'</div></div>';
    }});
    
    if (data.strength) data.strength.forEach((s, si) => {{
      detailHTML += '<div class="detail-card strength-border"><h2>💪 '+fmtMD(d)+' '+wd+' · '+s.name+'</h2>'
        +'<p style="color:#888;font-size:13px;margin-bottom:16px;">总时长 '+secToHMS(s.time)+' · 消耗 '+s.kcal+'kcal</p>';
      s.exercises.forEach((ex,i) => {{
        detailHTML += '<div class="exercise-card"><div class="ex-name">'+(i+1)+'. '+ex.name+'</div><div class="sets-grid">';
        ex.sets.forEach(set => {{
          if (set.wt && set.rep) detailHTML += '<div class="set-badge"><span class="wt">'+set.wt+'</span> &times; <span class="rp">'+set.rep+'</span></div>';
        }});
        detailHTML += '</div></div>';
      }});
      detailHTML += '</div>';
    }});
  }});
  document.getElementById("detailSection").innerHTML = detailHTML || '<div class="card empty-state"><p>本周暂无训练记录</p></div>';

  let analysis = '';
  if (isCurrentWeek) {{
    const plan = ["力量日","有氧日","力量日","有氧日","力量日","有氧日","休息"];
    analysis = '<strong>📋 本周计划对照（一三五力量 / 二四六有氧）：</strong><br>';
    days.forEach((d,i) => {{
      if (d > today) {{ analysis += '⬜ '+fmtMD(d)+' '+WEEKDAYS[i]+'：'+plan[i]+'<br>'; return; }}
      const k = fmtDate(d);
      const data = TRAINING_DATA[k];
      const hasCardio = data?.cardio?.length;
      const hasStrength = data?.strength?.length;
      const actual = hasCardio && hasStrength ? 'both' : hasCardio ? '有氧' : hasStrength ? '力量('+data.strength.length+'场)' : '未练';
      const icon = actual==='未练'?'❌':(plan[i].includes('力量')&&actual.startsWith('有氧')||plan[i].includes('有氧')&&actual.startsWith('力量'))?'🔄':'✅';
      analysis += icon+' '+fmtMD(d)+' '+WEEKDAYS[i]+'：'+actual+(actual==='未练'?'（缺）':'')+'<br>';
    }});
    analysis += '<br><strong>💡 提示：</strong><br>'
      +'· 全量数据范围: '+firstDataDate+' → '+lastDataDate+'，共 '+allDates.length+' 天<br>'
      +'· 力量训练数据来自你在训记 App 手动录入<br>'
      +'· COROS 手表只记录有氧（跑步/骑行/爬楼梯）<br>'
      +'· 每日自动由 GitHub Actions 更新';
  }} else {{
    analysis = '<strong>📊 历史周回顾</strong><br>'
      +'· 这是 '+fmtMD(monday)+' 到 '+fmtMD(sunday)+' 的训练数据<br>'
      +'· 使用 ◂ ▸ 箭头回到本周';
  }}
  document.getElementById("analysisBox").innerHTML = analysis;
}}

function prevWeek() {{ viewMonday.setDate(viewMonday.getDate()-7); render(); }}
function nextWeek() {{
  const next = new Date(viewMonday);
  next.setDate(next.getDate()+7);
  if (next <= getMonday(today)) {{ viewMonday = next; render(); }}
}}
function hideCards() {{
  document.getElementById("overviewCard").classList.add("collapsed");
  document.getElementById("weekViewCard").classList.add("collapsed");
  document.getElementById("floatingRed").classList.add("show");
}}
function restoreCards() {{
  document.getElementById("overviewCard").classList.remove("collapsed");
  document.getElementById("weekViewCard").classList.remove("collapsed");
  document.getElementById("floatingRed").classList.remove("show");
}}
function openDiet() {{
  document.getElementById("dietModal").classList.add("show");
}}
function closeDiet() {{ document.getElementById("dietModal").classList.remove("show"); }}
document.getElementById("dietModal").addEventListener("click",function(e){{ if(e.target===this)closeDiet(); }});

let expanded = false;
function toggleExpand() {{
  const cards = document.querySelectorAll("#detailSection .detail-card");
  expanded = !expanded;
  cards.forEach(c => {{ c.style.maxHeight=expanded?"none":"200px"; c.style.overflow=expanded?"visible":"hidden"; }});
}}

render();
</script>

<p style="color:#555;font-size:12px;margin-top:16px;text-align:center;">
  数据来自训记 API · 共 {len(dates)} 天训练记录 · 由 GitHub Actions 每日自动更新<br>
  数据范围: {first_date} → {last_date}
</p>
</body>
</html>"""
    )

print(f"\nOK - train_report.html 已生成 ({len(dates)} 天)")
