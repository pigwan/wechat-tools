"""定时提醒核心逻辑：口语化时间解析、违禁词检查、循环排程。

纯 Python 标准库，不依赖任何第三方包或大模型。被 report_bridge 插件加载，
也可单独导入做单元测试。

设计要点：
  - 所有时间按本机本地时区计算，落库用 epoch 秒（float），避免时区歧义。
  - 解析策略：先在文本里定位所有「时间片段」并记录字符区间，解析完后统一
    删除这些区间，剩下的正文就是提醒内容 —— 避免日期/时刻词混进正文。
  - 违禁词库来自项目 data/ 目录，匹配前做归一化（全角转半角、去标点空格、
    小写），防止简单的插入字符绕过。
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timedelta

MIN_INTERVAL_SEC = 300        # 最短「循环」间隔：两次触发之间不得少于 5 分钟
# 投递方式由「指令来自哪里」决定，跟提醒时间/间隔无关：
#   群里 @ 发起 -> 原群回复并 @；私聊发起 -> 私信回复那个人（不带 @）。

TRIGGER_WORDS = ("提醒", "定时", "叫我", "记得", "闹钟", "到点", "提醒我", "提醒一下")

CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
WEEK_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
WEEK_LABEL = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}

# 明确的违法违规行为短语（词库之外补充，命中同样拒绝）
ILLEGAL_BEHAVIOR = (
    "赌博", "开设赌场", "聚众赌博", "网络赌博", "私彩", "外围投注",
    "诈骗", "电信诈骗", "刷单", "杀猪盘", "非法集资", "集资诈骗", "传销",
    "贩毒", "制毒", "运毒", "吸毒", "毒品交易", "冰毒", "海洛因",
    "洗钱", "跑分", "买卖银行卡", "出租银行卡", "贩卖个人信息", "倒卖公民信息",
    "代孕", "买卖枪支", "非法持枪", "贩卖枪支", "制造炸药", "私制爆炸物",
    "卖淫", "嫖娼", "组织卖淫", "传播淫秽", "制作淫秽", "儿童色情",
    "行贿", "受贿", "贪污", "虚开发票", "代开发票", "偷税漏税", "逃税",
    "绑架", "勒索", "故意伤害", "雇凶", "买凶", "人肉搜索", "网络暴力",
    "伪造证件", "办假证", "伪造公章", "套牌", "走私", "偷渡", "蛇头",
    "黑客攻击", "入侵系统", "盗号", "木马", "勒索病毒", "非法挖矿",
    "高利贷", "套路贷", "暴力催收", "裸贷", "校园贷诈骗",
    "危险驾驶", "毒驾", "醉驾逃逸", "肇事逃逸",
)

# 合法但容易被词库误伤的中性词，放行。
# 腾讯词库把一批日常词也单列成了词条：第 21078~21090 行是「广告/违禁品交易」
# 词区（手机/电话/联系/出售/制作…），第 1538、45163 行「测试」夹在一堆 test
# 大小写变体里。定时提醒里这些字极常用，按精确词条剔除；含这些字的真违禁复合词
# （手机窃听、出售手机窃听器、制造炸药…）仍是独立词条/在 ILLEGAL_BEHAVIOR 里，不受影响。
LEXICON_ALLOW = frozenset({
    "提醒", "开会", "会议", "交周报", "周报", "日报", "吃饭", "喝水",
    "吃药", "打卡", "下班", "上班", "接孩子", "取快递", "缴费", "还款",
    "生日", "纪念日", "体检", "复查", "疫苗", "健身", "运动", "睡觉",
    "手机", "电话", "测试", "联系", "销售", "出售", "收购", "求购",
    "制作", "制造", "自制", "购买", "地址",
})


# --------------------------------------------------------------------------
# 文本归一化 / 违禁词
# --------------------------------------------------------------------------

def normalize(text):
    """全角转半角、小写、丢弃所有非字母数字与汉字字符。"""
    if not text:
        return ""
    out = []
    for ch in unicodedata.normalize("NFKC", str(text)).lower():
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            out.append(ch)
    return "".join(out)


def load_lexicon(paths):
    """读取多个词库文件，返回按首字符分桶的词条字典（避免全量扫描）。"""
    words = {}
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    word = normalize(line.strip())
                    if 2 <= len(word) <= 30 and word not in LEXICON_ALLOW:
                        words.setdefault(word[0], set()).add(word)
        except OSError:
            continue
    for phrase in ILLEGAL_BEHAVIOR:
        word = normalize(phrase)
        words.setdefault(word[0], set()).add(word)
    return words


def find_banned_all(text, lexicon, limit=None):
    """返回所有命中的违禁词条（归一化后），按在正文中出现的先后排序。

    同一位置优先列更长的词（更具体）。limit 限制条数防止刷屏，None 不限制。
    """
    norm = normalize(text)
    if not norm:
        return []
    hits = {}
    for ch in set(norm):
        for word in lexicon.get(ch, ()):
            pos = norm.find(word)
            if pos >= 0 and (word not in hits or pos < hits[word]):
                hits[word] = pos
    ordered = [w for w, _ in sorted(hits.items(), key=lambda kv: (kv[1], -len(kv[0])))]
    # 同一段文字常被多个词条重复命中（「出售手机窃听器」会同时命中「出售」
    # 「窃听」「手机窃听」「窃听器」…），只保留不被其它命中词包含的最长词，
    # 避免回复里刷一长串同义碎片；互不相关的多个词仍然全部列出。
    ordered = [w for w in ordered
               if not any(w != other and w in other for other in ordered)]
    return ordered[:limit] if limit else ordered


def find_banned(text, lexicon):
    """返回最长的命中词条（归一化后），没有则 None。保留给旧调用方。"""
    hits = find_banned_all(text, lexicon)
    return max(hits, key=len) if hits else None


def mask_word(word):
    """逐字用 * 隔开，避免在提示里原样重发违禁词：手机 -> 手*机。"""
    return "*".join(str(word or ""))


def format_banned_reply(hits):
    """拦截提示：把命中的词打码后直接列出来，方便知道该改哪几个字。"""
    if not hits:
        # 刻意与旧版文案区分：以后再看到「违法违规信息」字样，说明跑的是旧副本
        return "❌ 内容触发违禁词，无法设置提醒。"
    words = "、".join("【%s】" % mask_word(w) for w in hits if w)
    return "❌ 内容触发违禁词：%s，无法设置提醒。" % words


# --------------------------------------------------------------------------
# 中文数字 / 时刻
# --------------------------------------------------------------------------

def cn2num(token):
    """把 '5' / '十五' / '二十三' / '两' 转成数字，失败返回 None。"""
    if token is None:
        return None
    s = str(token).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s == "半":
        return 0.5
    if s == "十":
        return 10
    if "十" in s:
        left, _, right = s.partition("十")
        tens = CN_DIGITS.get(left, 1) if left else 1
        ones = CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(s) == 1 and s in CN_DIGITS:
        return CN_DIGITS[s]
    if all(ch in CN_DIGITS for ch in s):
        return int("".join(str(CN_DIGITS[ch]) for ch in s))
    return None


# 数值 token 不含「半」——「半」在中文里是特殊词，放进通用数值会吞掉
# 「半小时」「3点半」等表达，必须由专门的分支处理。
NUM = r"(?:\d+|[零〇一二两三四五六七八九十百]+)"
PERIOD = r"(?:凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里)?"
CLOCK_BODY = r"(?:\d{1,2}:\d{1,2}|%s\s*点(?:\s*(?:半|一刻|三刻)|\s*%s\s*分?)?)" % (NUM, NUM)
CLOCK_RE = re.compile(PERIOD + r"\s*" + CLOCK_BODY)


def _mk_dt(base, hh, mm):
    return base.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)


def parse_clock(token, now=None):
    """把 '9点' / '9点半' / '15:30' / '下午3点' / '晚上8点' 解析成 (时, 分)。"""
    if not token:
        return None
    t = unicodedata.normalize("NFKC", str(token)).replace("：", ":")
    period = None
    for label in ("凌晨", "早上", "早晨", "上午", "中午", "下午", "傍晚", "晚上", "夜里"):
        if label in t:
            period = label
            t = t.replace(label, "")
            break
    t = t.strip()
    hh = mm = None
    m = re.match(r"^(\d{1,2}):(\d{1,2})$", t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
    if hh is None:
        m = re.match(r"^(%s)\s*点(?:\s*(半|一刻|三刻)|\s*(%s)\s*分?)?" % (NUM, NUM), t)
        if m:
            hh = cn2num(m.group(1))
            special = m.group(2)
            if special == "半":
                mm = 30
            elif special == "一刻":
                mm = 15
            elif special == "三刻":
                mm = 45
            elif m.group(3) is not None:
                mm = int(cn2num(m.group(3)) or 0)
            else:
                mm = 0
    if hh is None:
        return None
    hh = int(hh)
    if period in ("下午", "傍晚", "晚上", "夜里") and hh < 12:
        hh += 12
    if period == "中午" and hh < 11:
        hh += 12
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def _weekday_cn_to_ints(text):
    result = []
    for ch in text:
        if ch in WEEK_CN:
            value = WEEK_CN[ch]
            if value not in result:
                result.append(value)
    return sorted(result) or [1]


# --------------------------------------------------------------------------
# 主解析入口
# --------------------------------------------------------------------------

def parse_command(raw_text, now=None):
    """解析一条提醒指令，返回可直接写入 reminders 表的字段字典。

    ok=False 时只带 error。成功时含：
      kind / rule_json / rule_text / interval_sec / weekdays / at_hhmm
      next_ts / span_sec / content / raw_text

    注意：delivery 不在这里决定，由调用方按「消息来自群还是私聊」设置。
    MIN_INTERVAL_SEC 只约束循环任务「两次触发的间隔」，不约束一次性提醒
    距离现在有多近（「3分钟后提醒我喝水」是合法的）。
    """
    now = now or datetime.now().astimezone()
    text = unicodedata.normalize("NFKC", str(raw_text or "")).replace("：", ":")
    # 去掉 @昵称 标签（微信用 \u2005 或空格分隔），保留正文
    text = re.sub(r"@[^\s@\u2005]+\u2005?", "", text)
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return {"ok": False, "error": "没读到提醒内容"}

    # 去掉触发词，避免污染正文
    cleaned = text
    for word in ("提醒一下", "提醒我", "提醒", "定时", "叫我", "记得", "闹钟", "到点"):
        cleaned = cleaned.replace(word, " ", 1)
    cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()

    result = _parse_time(cleaned, now)
    if not result.get("ok"):
        return result
    content = re.sub(r"^[，,。.、\s:：-]+|[，,。.、\s:：-]+$", "",
                     result.pop("content")).strip()
    if not content:
        return {"ok": False, "error": "没读到要提醒的内容，例如：@机器人 提醒 明天9点 交周报"}
    if len(content) > 500:
        return {"ok": False, "error": "提醒内容太长（最多 500 字）"}
    result["content"] = content
    result["raw_text"] = str(raw_text or "")

    # 5 分钟下限只管「循环两次触发之间的间隔」；一次性提醒不受限制。
    if result["kind"] == "interval" and int(result["interval_sec"] or 0) < MIN_INTERVAL_SEC:
        return {"ok": False, "error": "循环间隔最短 5 分钟，低于 5 分钟的设置不支持"}
    result["rule_json"] = json.dumps({
        "kind": result["kind"],
        "interval_sec": result["interval_sec"],
        "weekdays": result["weekdays"],
        "at_hhmm": result["at_hhmm"],
    }, ensure_ascii=False)
    return result


def _cut(text, spans):
    """删除所有被消费的字符区间，剩余即提醒正文。"""
    merged = []
    for start, end in sorted(spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    out, pos = [], 0
    for start, end in merged:
        out.append(text[pos:start])
        pos = end
    out.append(text[pos:])
    return re.sub(r"[ \t]+", " ", " ".join(out)).strip()


def _take_clock(text, spans, now, default=None, search_from=0):
    """在 text 中找时刻表达式，登记区间并返回 (hh, mm)。"""
    m = CLOCK_RE.search(text, search_from)
    if m:
        hhmm = parse_clock(m.group(0), now)
        if hhmm:
            spans.append((m.start(), m.end()))
            return hhmm
    return default


def _parse_time(text, now):
    """识别时间表达式，返回带 content 的中间结果。"""
    spans = []

    # ---------- 循环：每 N 分钟 / 每 N 小时 / 每 N 天 ----------
    m = re.search(r"每\s*(?:隔)?\s*(%s)\s*(分钟|分|小时|个小时|钟头|天|日)" % NUM, text)
    if m:
        value, unit = cn2num(m.group(1)), m.group(2)
        if value is None:
            return {"ok": False, "error": "没看懂时间：%s" % m.group(0)}
        if unit in ("分钟", "分"):
            sec, label = int(value * 60), "每 %g 分钟" % value
        elif unit in ("小时", "个小时", "钟头"):
            sec, label = int(value * 3600), "每 %g 小时" % value
        else:
            sec, label = int(value * 86400), "每 %g 天" % value
        if sec < MIN_INTERVAL_SEC:
            return {"ok": False, "error": "最短支持每 5 分钟，低于 5 分钟的设置不支持"}
        spans.append((m.start(), m.end()))
        return {
            "ok": True, "kind": "interval", "interval_sec": sec,
            "weekdays": [], "at_hhmm": "", "rule_text": label,
            "span_sec": sec, "next_ts": now.timestamp() + sec,
            "content": _cut(text, spans),
        }

    # ---------- 循环：每半小时 / 每小时 / 每分钟 / 每天 等无数字写法 ----------
    m = re.search(r"每\s*(半小时|半个钟头|一小时|一个钟头|小时|钟头|分钟)", text)
    if m:
        token = m.group(1)
        if token in ("半小时", "半个钟头"):
            sec, label = 1800, "每 30 分钟"
        elif token in ("一小时", "一个钟头", "小时", "钟头"):
            sec, label = 3600, "每 1 小时"
        else:
            return {"ok": False, "error": "最短支持每 5 分钟，低于 5 分钟的设置不支持"}
        if sec < MIN_INTERVAL_SEC:
            return {"ok": False, "error": "最短支持每 5 分钟，低于 5 分钟的设置不支持"}
        spans.append((m.start(), m.end()))
        return {
            "ok": True, "kind": "interval", "interval_sec": sec,
            "weekdays": [], "at_hhmm": "", "rule_text": label,
            "span_sec": sec, "next_ts": now.timestamp() + sec,
            "content": _cut(text, spans),
        }

    # ---------- 循环：每周 X / 每星期 X ----------
    m = re.search(r"每\s*(?:周|星期|礼拜)\s*([一二三四五六日天]+(?:[、,，和及][一二三四五六日天]+)*)", text)
    if m:
        days = _weekday_cn_to_ints(m.group(1))
        spans.append((m.start(), m.end()))
        hhmm = _take_clock(text, spans, now, default=(9, 0), search_from=m.end())
        label = "每周%s %02d:%02d" % ("、".join(WEEK_LABEL[d] for d in days), hhmm[0], hhmm[1])
        return {
            "ok": True, "kind": "weekly", "interval_sec": 0,
            "weekdays": days, "at_hhmm": "%02d:%02d" % hhmm,
            "rule_text": label, "span_sec": 7 * 86400,
            "next_ts": _next_weekly(now, days, hhmm),
            "content": _cut(text, spans),
        }

    # ---------- 循环：工作日 ----------
    m = re.search(r"(?:每\s*)?(?:个)?\s*工作日", text)
    if m:
        spans.append((m.start(), m.end()))
        hhmm = _take_clock(text, spans, now, default=(9, 0), search_from=m.end())
        return {
            "ok": True, "kind": "weekly", "interval_sec": 0,
            "weekdays": [1, 2, 3, 4, 5], "at_hhmm": "%02d:%02d" % hhmm,
            "rule_text": "工作日 %02d:%02d" % hhmm, "span_sec": 86400,
            "next_ts": _next_weekly(now, [1, 2, 3, 4, 5], hhmm),
            "content": _cut(text, spans),
        }

    # ---------- 循环：每天 / 每日 ----------
    m = re.search(r"每\s*(天|日)", text)
    if m:
        spans.append((m.start(), m.end()))
        hhmm = _take_clock(text, spans, now, default=(9, 0), search_from=m.end())
        return {
            "ok": True, "kind": "daily", "interval_sec": 0,
            "weekdays": [], "at_hhmm": "%02d:%02d" % hhmm,
            "rule_text": "每天 %02d:%02d" % hhmm, "span_sec": 86400,
            "next_ts": _next_daily(now, hhmm),
            "content": _cut(text, spans),
        }

    # ---------- 一次性：一个半小时后 / 半小时后 ----------
    m = re.search(r"(一个半|半|半个|1\.5)\s*(?:小时|钟头)\s*(?:之|以)?后", text)
    if m:
        sec = 5400 if m.group(1) in ("一个半", "1.5") else 1800
        spans.append((m.start(), m.end()))
        return {
            "ok": True, "kind": "once", "interval_sec": 0,
            "weekdays": [], "at_hhmm": "", "rule_text": "一次性",
            "span_sec": sec, "next_ts": now.timestamp() + sec,
            "content": _cut(text, spans),
        }

    # ---------- 一次性：N 分钟/小时/天后 ----------
    m = re.search(r"(%s)\s*(分钟|分|小时|个小时|钟头|天|日)\s*(?:之|以)?后" % NUM, text)
    if m:
        value, unit = cn2num(m.group(1)), m.group(2)
        if value is None:
            return {"ok": False, "error": "没看懂时间：%s" % m.group(0)}
        if unit in ("分钟", "分"):
            sec = int(value * 60)
        elif unit in ("小时", "个小时", "钟头"):
            sec = int(value * 3600)
        else:
            sec = int(value * 86400)
        spans.append((m.start(), m.end()))
        return {
            "ok": True, "kind": "once", "interval_sec": 0,
            "weekdays": [], "at_hhmm": "", "rule_text": "一次性",
            "span_sec": sec, "next_ts": now.timestamp() + sec,
            "content": _cut(text, spans),
        }

    # ---------- 一次性：日期 + 时刻 ----------
    return _parse_absolute(text, now, spans)


DATE_WORDS = {
    "大后天": 3, "后天": 2,
    "今天": 0, "今日": 0, "今晚": 0, "今早": 0,
    "明天": 1, "明日": 1, "明晚": 1, "明早": 1, "明儿": 1,
}


def _parse_absolute(text, now, spans):
    base = None
    date_word = None
    date_end = 0

    # 下周一 / 本周三 / 周三
    m = re.search(r"(下下|下|本|这)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天])", text)
    if m:
        target = WEEK_CN.get(m.group(2), 1)
        delta = (target - 1 - now.weekday()) % 7      # 最近的未来（含今天）
        prefix = m.group(1) or ""
        if prefix == "下下":
            delta += 7
        if delta == 0:
            delta = 7                                  # 「周三」当天已不算提醒
        base = now + timedelta(days=delta)
        date_end, date_word = m.end(), m.group(0)
        spans.append((m.start(), m.end()))
    else:
        for word, offset in sorted(DATE_WORDS.items(), key=lambda kv: -len(kv[0])):
            if word in text:
                base = now + timedelta(days=offset)
                date_end = text.index(word) + len(word)
                date_word = word
                spans.append((text.index(word), date_end))
                break
        if base is None:
            m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?", text)
            if m:
                try:
                    base = now.replace(month=int(m.group(1)), day=int(m.group(2)))
                except ValueError:
                    return {"ok": False, "error": "日期不合法：%s" % m.group(0)}
                if base.date() < now.date():
                    try:
                        base = base.replace(year=now.year + 1)
                    except ValueError:
                        return {"ok": False, "error": "日期不合法：%s" % m.group(0)}
                date_end, date_word = m.end(), m.group(0)
                spans.append((m.start(), m.end()))
            else:
                m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
                if m:
                    try:
                        base = now.replace(year=int(m.group(1)), month=int(m.group(2)),
                                           day=int(m.group(3)))
                    except ValueError:
                        return {"ok": False, "error": "日期不合法：%s" % m.group(0)}
                    date_end, date_word = m.end(), m.group(0)
                    spans.append((m.start(), m.end()))

    hhmm = _take_clock(text, spans, now, search_from=date_end)
    if base is None and hhmm is None:
        return {"ok": False, "error": "没识别出提醒时间，试试「明天9点」「30分钟后」「每周一 10点」"}

    if hhmm is None:
        hhmm = (20, 0) if date_word and "晚" in str(date_word) else (9, 0)
    # 「今晚8点」「明晚7点」：日期词已含晚上意味，钟点需 +12
    if date_word and "晚" in str(date_word) and hhmm[0] < 12:
        hhmm = (hhmm[0] + 12, hhmm[1])

    # 显式带了上午时段词（凌晨/早上/早晨/上午/中午）时，不要做下面的「下午优先」推断。
    explicit_am = any(w in text for w in ("凌晨", "早上", "早晨", "上午", "中午"))

    base = base or now
    target_dt = _mk_dt(base, hhmm[0], hhmm[1])
    if target_dt <= now:
        # 只写时刻没写日期，或写的是今天但时刻已过 -> 顺延
        if hhmm[0] < 12 and not explicit_am:
            # 没有上午/下午限定词的钟点：下午或晚上说「5点」通常指 17:00 而非凌晨 05:00。
            # 今天 h:00 已过，先试同日的 h+12（下午），仍过才顺延到明天 h:00。
            pm = _mk_dt(base, hhmm[0] + 12, hhmm[1])
            target_dt = pm if pm > now else target_dt + timedelta(days=1)
        else:
            target_dt += timedelta(days=1)
    sec = int(target_dt.timestamp() - now.timestamp())

    return {
        "ok": True, "kind": "once", "interval_sec": 0,
        "weekdays": [], "at_hhmm": "", "rule_text": "一次性 %s" % target_dt.strftime("%m-%d %H:%M"),
        "span_sec": sec, "next_ts": target_dt.timestamp(),
        "content": _cut(text, spans),
    }


# --------------------------------------------------------------------------
# 循环下次触发时间
# --------------------------------------------------------------------------

def _next_daily(now, hhmm):
    target = _mk_dt(now, hhmm[0], hhmm[1])
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


def _next_weekly(now, weekdays, hhmm):
    for offset in range(0, 15):
        day = now + timedelta(days=offset)
        if (day.weekday() + 1) in weekdays:
            target = _mk_dt(day, hhmm[0], hhmm[1])
            if target > now:
                return target.timestamp()
    return (now + timedelta(days=7)).timestamp()


def next_fire(rule_json, after_ts=None, now=None):
    """计算循环任务的下一次触发时间；一次性任务返回 None。"""
    now = now or datetime.now().astimezone()
    try:
        rule = json.loads(rule_json or "{}")
    except (ValueError, TypeError):
        return None
    kind = rule.get("kind")
    if kind == "once" or not kind:
        return None
    base_ts = max(after_ts or 0, now.timestamp())
    if kind == "interval":
        sec = max(int(rule.get("interval_sec") or 0), MIN_INTERVAL_SEC)
        return base_ts + sec
    hhmm = rule.get("at_hhmm") or "09:00"
    try:
        hh, mm = (int(x) for x in str(hhmm).split(":"))
    except ValueError:
        hh, mm = 9, 0
    base_dt = datetime.fromtimestamp(base_ts, now.tzinfo)
    if kind == "daily":
        return _next_daily(base_dt, (hh, mm))
    if kind == "weekly":
        days = [int(x) for x in (rule.get("weekdays") or [1])]
        return _next_weekly(base_dt, days, (hh, mm))
    return None


def build_delivery(row):
    """把一条 reminders 行格式化成 outbox 记录，插件与管理台共用。

    row 是 dict，至少含 content / targets / target_names / delivery /
    chat_id / dm_to。返回 dict(chat_id, text, at_list, at_names)。

    投递方式跟随指令来源：
      group -> 回原群，带 @ 名单（框架会拼 @昵称）
      dm    -> 回原私聊会话（dm_to，通常就是 chat_id），不带 @
    """
    content = str(row.get("content") or "").strip()
    targets = str(row.get("targets") or "").strip()
    target_names = str(row.get("target_names") or "").strip()
    delivery = str(row.get("delivery") or "group")
    if delivery == "dm":
        # 私聊里没有 @ 的概念，直接把提醒内容发给原会话
        dm_to = (str(row.get("dm_to") or "").strip()
                 or str(row.get("chat_id") or "").strip())
        return {
            "chat_id": dm_to,
            "text": "⏰ 定时提醒\n" + content,
            "at_list": "",
            "at_names": "",
        }
    return {
        "chat_id": str(row.get("chat_id") or ""),
        "text": "⏰ " + content,
        "at_list": targets,
        "at_names": target_names,
    }


def describe(rule_json, delivery):
    """给列表页用的简短描述。"""
    try:
        rule = json.loads(rule_json or "{}")
    except (ValueError, TypeError):
        rule = {}
    kind = rule.get("kind", "once")
    parts = [{"once": "一次性", "interval": "按间隔循环",
              "daily": "每天", "weekly": "每周"}.get(kind, kind)]
    if kind == "interval":
        sec = int(rule.get("interval_sec") or 0)
        parts.append(("%g 分钟" % (sec / 60)) if sec < 3600 else ("%g 小时" % (sec / 3600)))
    if rule.get("at_hhmm"):
        parts.append(rule["at_hhmm"])
    if rule.get("weekdays"):
        parts.append("周" + "、".join(WEEK_LABEL.get(d, str(d)) for d in rule["weekdays"]))
    parts.append("群里@" if delivery != "dm" else "私信")
    return " · ".join(parts)
