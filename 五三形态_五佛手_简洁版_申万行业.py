# -*- coding: utf-8 -*-
"""
========================================================
Tushare 前复权稳定版：A股日线 五佛手简洁选股脚本
========================================================

本版只保留五佛手本体判断：
1) 行情取数沿用稳定的 pro.daily 方式，并用 adj_factor 手动计算前复权 qfq。
2) target day 就是五佛手成立当天。
3) B2 / B3 / B4 / B5 量能确认逻辑全部删除。

输出文件：
- 五佛手主结果池
- lite 精简结果
- debug / failed / filtered 文件
"""

import os
import time
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import tushare as ts


# =========================
# 清理代理
# =========================
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)


# =========================
# 本地文件路径：按你的习惯保留，可自行修改
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))
OUTPUT_DIR = os.getenv("OUTPUT_DIR", os.path.join(BASE_DIR, "outputs"))

CODE_FILE = os.getenv("CODE_FILE", os.path.join(DATA_DIR, "a_share_codes_for_akshare.csv"))
BELOW_8B_FILE = os.getenv("BELOW_8B_FILE", os.path.join(DATA_DIR, "a_share_below_8b.csv"))
ST_FILE = os.getenv("ST_FILE", os.path.join(DATA_DIR, "st_stocks.csv"))


# =========================
# Tushare Token
# =========================
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "").strip()


def shanghai_today_str() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")


# =========================
# 日期控制：多日期
# =========================
# 方式1：直接列出多个目标日。支持字符串、整数、逗号分隔字符串。
# 例：TARGET_DATES_INPUT = ["20260430", "20260506"]
# 例：TARGET_DATES_INPUT = "20260430,20260506,20260507"
TARGET_DATES_ENV = os.getenv("TARGET_DATES", "").strip()
TARGET_DATES_INPUT = TARGET_DATES_ENV or []

# 方式2：按自然日范围批量生成目标日。非交易日会在 debug 文件中记录 target_date_not_trading_day。
#TARGET_DATE_RANGES = [("20260105", "20260105")]
TARGET_DATE_RANGES_ENV = os.getenv("TARGET_DATE_RANGES", "").strip()
TARGET_DATE_RANGES = []


def parse_target_date_ranges(raw: str):
    ranges = []
    if not raw:
        return ranges
    for piece in raw.split(","):
        part = piece.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(f"TARGET_DATE_RANGES 格式错误: {part}")
        start, end = [x.strip() for x in part.split(":", 1)]
        ranges.append((start, end))
    return ranges


if TARGET_DATE_RANGES_ENV:
    TARGET_DATE_RANGES = parse_target_date_ranges(TARGET_DATE_RANGES_ENV)
#TARGET_DATES_INPUT = []

def normalize_target_dates(target_dates_input, target_date_ranges=None):
    dates = []

    def add_one(x):
        if x is None:
            return
        s = str(x).strip()
        if not s:
            return
        if "," in s:
            for part in s.split(","):
                add_one(part)
            return
        s = s.replace("-", "").replace("/", "")
        if len(s) != 8 or not s.isdigit():
            raise ValueError(f"目标日期格式错误: {x}，请使用 YYYYMMDD，例如 20260506")
        pd.to_datetime(s, format="%Y%m%d")
        dates.append(s)

    if isinstance(target_dates_input, (list, tuple, set)):
        for item in target_dates_input:
            add_one(item)
    else:
        add_one(target_dates_input)

    for start, end in (target_date_ranges or []):
        start_dt = pd.to_datetime(str(start).replace("-", "").replace("/", ""), format="%Y%m%d")
        end_dt = pd.to_datetime(str(end).replace("-", "").replace("/", ""), format="%Y%m%d")
        if end_dt < start_dt:
            raise ValueError(f"目标日期范围错误: {start} 到 {end}")
        dates.extend(pd.date_range(start_dt, end_dt, freq="D").strftime("%Y%m%d").tolist())

    dates = sorted(set(dates))
    if not dates:
        raise ValueError("TARGET_DATES 不能为空，请至少设置一个目标日。")
    return dates


TARGET_DATES = []
TARGET_DATES_TAG = ""
OBSERVATION_FILE = ""
DEBUG_FILE = ""
FAILED_FILE = ""
FILTERED_FILE = ""
OBSERVATION_LITE_FILE = ""
# SCAN_END_DATE 只代表需要扫描/输出的最后一个 target day。
SCAN_END_DATE = ""
START_DATE = "20240101"

# 前复权锚定日期：
# 关键修复点：如果只把行情取到 target day，例如 20260506，
# 而除权除息发生在 20260508，那么 20260506 之前的价格不会被压到当前前复权口径，
# 所以寒武纪仍会显示 1800+。
# 因此，前复权需要把 adj_factor 至少取到除权除息之后，通常取到今天或你指定的锚定日。
# - None：默认使用当前日期作为前复权锚定日。
# - 也可以手动写死，例如 "20260520"，保证历史复盘结果可复现。
PRICE_ADJ_ANCHOR_DATE = os.getenv("PRICE_ADJ_ANCHOR_DATE", "").strip()

# 实际取数截止日：必须覆盖 target day，同时也要覆盖前复权锚定日。
# 后续信号仍只在 TARGET_DATES 上判断，不会把锚定日之后的K线当作目标日扫描。
FETCH_END_DATE = ""
END_DATE = ""  # 保留兼容旧变量名，表示扫描结束日，不再用于取数截止


# =========================
# 运行参数
# =========================
MAX_WORKERS = 1
TEST_LIMIT = None
BATCH_START = 0
BATCH_SIZE = 10000
SLEEP_SEC = 0.10
MIN_HISTORY_BARS = 260

# 前复权模式：qfq 前复权；None 不复权；hfq 后复权
PRICE_ADJ_MODE = "qfq"  # 稳定版：不使用 ts.pro_bar，而是 pro.daily + pro.adj_factor 手动复权


# =========================
# 五佛手参数
# =========================
FB_LOOKBACK = 15
FB_RECENT_CONVERGE_MAX = 0.12       # 最近15日内五线最大间隔至少曾经 <= 12%
FB_RECENT_CONVERGE_MAX_IF_MIN_IS_MA60 = 0.16  # 如果最小均线是MA60，则最近15日最小离散度放宽到 <= 16%
FB_CURRENT_SPREAD_MAX = 0.20        # target day五线最大间隔不能超过20%
FB_CURRENT_SPREAD_MAX_IF_MIN_IS_MA60 = 0.25  # 如果target day最低均线是MA60，则当天五线最大间隔放宽到 <= 25%
FB_MAX_CLOSE_OVER_MA20 = 0.20       # 收盘价离MA20太远则不进入形态主池
FB_MAX_CLOSE_OVER_MA20_IF_MIN_IS_MA60 = 0.30  # 如果target day最低均线是MA60，则close相对MA20最大偏离放宽到 <= 30%
FB_REQUIRE_CLOSE_ABOVE_ALL_MA = True

FB_MA30_3D_FLOOR = -0.010
FB_MA60_5D_FLOOR = -0.015
FB_NEW_HIGH_LOOKBACK = 30
FB_TURNOVER_RATE_MIN = 1.5
FB_REQUIRE_LAST3_VOL_ABOVE_MA20 = True

FB_CONFIRM_LOOKBACK_DAYS = 5        # 买点确认日向前回看最近几根K线内是否有五佛手，含确认日

# =========================
# 申万一级行业参数
# =========================
SW_INDUSTRY_SRC_CANDIDATES = ["SW2021", "SW"]
SW_INDUSTRY_LEVEL = "L1"
SW_INDUSTRY_MA_N = 20

SW_INDEX_MEMBER_CACHE = {}
SW_INDEX_MEMBER_LOCK = threading.Lock()
SW_INDEX_DAILY_CACHE = {}
SW_INDEX_DAILY_LOCK = threading.Lock()


# =========================
# 6.3 买点确认参数
# =========================
B2_STRONG_UP_PCT = 0.040
B2_CLOSE_NEAR_HIGH_MAX = 0.25

B3_DAILY_DROP_FLOOR = -0.02
B4_DAILY_DROP_FLOOR = -0.02

B4_5D_TOTAL_UP_MIN = 0.03
B4_5D_TOTAL_UP_MAX = 0.08

B5_VOL_VS_MA20_STRICT = 2.50
B5_VOL_VS_MA20_LOOSE = 2.00
B5_2D_TOTAL_UP_STRICT = 0.06
B5_2D_TOTAL_UP_LOOSE = 0.04
B5_CLOSE_NEAR_HIGH_MAX = 0.35

BUY_POINT_RULESET = "book"
BUY_POINT_FILTER_REQUIRE_CLOSE_ABOVE_ALL_MA = True
BUY_POINT_FILTER_REQUIRE_TARGET_ABOVE_MA5 = True
BUY_POINT_FILTER_REQUIRE_TARGET_BULLISH_BAR = True
BUY_POINT_FILTER_REQUIRE_TARGET_POSITIVE_PCT = True
BUY_POINT_FILTER_TARGET_CLOSE_NEAR_HIGH_MAX = 0.45
FB_REQUIRE_MA30_MA60_MA250_BELOW_SHORT_MAS = True

B2_BOOK_VOL_RATIO_STRONG = 1.50
B2_BOOK_VOL_RATIO_BASIC = 1.20
B3_BOOK_VOL_EQUAL_TOL = 0.95
B4_BOOK_VOL_TAIL_HEAD_RATIO = 1.05
B5_BOOK_VOL_VS_MA20_BASIC = 1.60

GRADE_RANK = {"A": 1, "": 0}
TYPE_PRIORITY = {
    "B5_持续巨量上涨": 40,
    "B3_持续放量上涨": 30,
    "B2_放量上涨": 20,
    "B4_缓慢放量上涨": 10,
}


# =========================
# 工具函数
# =========================
def read_csv_safely(path: str) -> pd.DataFrame:
    encodings = ["utf-8", "utf-8-sig", "gbk", "gb18030"]
    last_err = None
    for enc in encodings:
        try:
            return pd.read_csv(path, encoding=enc, dtype=str)
        except Exception as e:
            last_err = e
    raise last_err


def normalize_to_6digits(x: str) -> str:
    s = str(x).strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) == 6:
        return digits
    if len(digits) > 6:
        return digits[-6:]
    if len(digits) > 0:
        return digits.zfill(6)
    return ""


def code6_to_ts_code(code6: str) -> str:
    if code6.startswith(("600", "601", "603", "605", "688", "900")):
        return f"{code6}.SH"
    if code6.startswith(("43", "83", "87", "88")):
        return f"{code6}.BJ"
    return f"{code6}.SZ"


def to_target_dt(target_date: str):
    return pd.to_datetime(str(target_date), format="%Y%m%d")


def safe_ratio(a, b, default=np.nan):
    try:
        a = float(a)
        b = float(b)
        if pd.isna(a) or pd.isna(b) or b == 0:
            return default
        return a / b
    except Exception:
        return default


def pct_gap(a: float, b: float, close_: float) -> float:
    if pd.isna(a) or pd.isna(b) or pd.isna(close_) or close_ == 0:
        return np.nan
    return (float(a) - float(b)) / float(close_)


def calc_change_vs_self(current: float, past: float) -> float:
    if pd.isna(current) or pd.isna(past) or past == 0:
        return np.nan
    return (float(current) - float(past)) / float(past)


def is_bullish_bar(row: pd.Series) -> bool:
    if pd.isna(row.get("open")) or pd.isna(row.get("close")):
        return False
    return bool(row["close"] >= row["open"])


def has_required_columns(df_like: pd.DataFrame, cols) -> bool:
    return not df_like[list(cols)].isna().any().any()


def avg_volume(series: pd.Series) -> float:
    if series.empty:
        return np.nan
    return float(series.mean())


def buy_point_filter_detail(row: pd.Series) -> dict:
    detail = {
        "buy_point_ruleset": BUY_POINT_RULESET,
        "buy_point_filter_require_close_above_all_ma": bool(BUY_POINT_FILTER_REQUIRE_CLOSE_ABOVE_ALL_MA),
        "buy_point_filter_require_target_above_ma5": bool(BUY_POINT_FILTER_REQUIRE_TARGET_ABOVE_MA5),
        "buy_point_filter_require_target_bullish_bar": bool(BUY_POINT_FILTER_REQUIRE_TARGET_BULLISH_BAR),
        "buy_point_filter_require_target_positive_pct": bool(BUY_POINT_FILTER_REQUIRE_TARGET_POSITIVE_PCT),
        "buy_point_filter_target_close_near_high_max": BUY_POINT_FILTER_TARGET_CLOSE_NEAR_HIGH_MAX,
        "buy_point_filter_reject_reason": "",
    }

    need_ma_cols = ["ma5", "ma10", "ma20", "ma30", "ma60", "ma250"]
    if BUY_POINT_FILTER_REQUIRE_CLOSE_ABOVE_ALL_MA:
        below = [c.upper() for c in need_ma_cols if pd.notna(row.get(c)) and row["close"] < row[c]]
        if below:
            detail["buy_point_filter_reject_reason"] = "BUY_FILTER_CLOSE_BELOW_" + "_".join(below)
            return detail

    if BUY_POINT_FILTER_REQUIRE_TARGET_ABOVE_MA5 and pd.notna(row.get("ma5")) and row["close"] < row["ma5"]:
        detail["buy_point_filter_reject_reason"] = "BUY_FILTER_CLOSE_BELOW_MA5"
        return detail

    if BUY_POINT_FILTER_REQUIRE_TARGET_BULLISH_BAR and not is_bullish_bar(row):
        detail["buy_point_filter_reject_reason"] = "BUY_FILTER_NOT_BULLISH_BAR"
        return detail

    if BUY_POINT_FILTER_REQUIRE_TARGET_POSITIVE_PCT and (pd.isna(row.get("pct_chg")) or row["pct_chg"] <= 0):
        detail["buy_point_filter_reject_reason"] = "BUY_FILTER_TARGET_PCT_NOT_POSITIVE"
        return detail

    if (pd.notna(row.get("close_near_high")) and
            row["close_near_high"] > BUY_POINT_FILTER_TARGET_CLOSE_NEAR_HIGH_MAX):
        detail["buy_point_filter_reject_reason"] = "BUY_FILTER_TARGET_CLOSE_NOT_NEAR_HIGH"
        return detail

    return detail


def ma_structure_detail(row: pd.Series) -> dict:
    short_ma_cols = ["ma5", "ma10"]
    lower_ma_cols = ["ma20", "ma30", "ma60", "ma250"]
    detail = {
        "ma_structure_rule_enabled": bool(FB_REQUIRE_MA30_MA60_MA250_BELOW_SHORT_MAS),
        "ma_structure_ok": False,
        "ma_structure_reason": "",
    }

    if any(pd.isna(row.get(c)) for c in short_ma_cols + lower_ma_cols):
        detail["ma_structure_reason"] = "MA_STRUCTURE_MISSING_REQUIRED_MA"
        return detail

    short_min = min(float(row[c]) for c in short_ma_cols)
    fail_cols = [c.upper() for c in lower_ma_cols if float(row[c]) >= short_min]
    detail["ma20_below_short_mas"] = bool(float(row["ma20"]) < short_min)
    detail["ma30_below_short_mas"] = bool(float(row["ma30"]) < short_min)
    detail["ma60_below_short_mas"] = bool(float(row["ma60"]) < short_min)
    detail["ma250_below_short_mas"] = bool(float(row["ma250"]) < short_min)
    detail["ma5_gt_ma10"] = bool(float(row["ma5"]) > float(row["ma10"]))

    if fail_cols:
        detail["ma_structure_reason"] = "MA_STRUCTURE_FAIL_" + "_".join(fail_cols) + "_NOT_BELOW_SHORT_MAS"
        return detail

    if not detail["ma5_gt_ma10"]:
        detail["ma_structure_reason"] = "MA_STRUCTURE_FAIL_NOT_MA5_GT_MA10"
        return detail

    detail["ma_structure_ok"] = True
    detail["ma_structure_reason"] = "MA_STRUCTURE_OK_MA5_GT_MA10_AND_MA20_MA30_MA60_MA250_BELOW_SHORT_MAS"
    return detail


def five_line_spread(row: pd.Series) -> float:
    mas = [row.get("ma5"), row.get("ma10"), row.get("ma20"), row.get("ma30"), row.get("ma60")]
    if any(pd.isna(x) for x in mas) or pd.isna(row.get("close")) or row.get("close") == 0:
        return np.nan
    return (max(mas) - min(mas)) / float(row["close"])


def min_ma_name_on_row(row: pd.Series) -> str:
    ma_vals = {
        "MA5": row.get("ma5", np.nan),
        "MA10": row.get("ma10", np.nan),
        "MA20": row.get("ma20", np.nan),
        "MA30": row.get("ma30", np.nan),
        "MA60": row.get("ma60", np.nan),
    }
    valid = [(name, float(val)) for name, val in ma_vals.items() if pd.notna(val)]
    if not valid:
        return ""
    return min(valid, key=lambda x: x[1])[0]


def recent_min_spread(df: pd.DataFrame, idx: int, lookback: int):
    start = max(0, idx - lookback + 1)
    vals = []
    for j in range(start, idx + 1):
        spread = df.iloc[j].get("five_line_spread", np.nan)
        if pd.notna(spread):
            vals.append((j, float(spread), min_ma_name_on_row(df.iloc[j])))
    if not vals:
        return np.nan, "", ""
    min_idx, min_val, min_ma_name = min(vals, key=lambda x: x[1])
    return min_val, df.iloc[min_idx]["date"].strftime("%Y-%m-%d"), min_ma_name


def ma_distance_grade_from_spread(spread: float) -> tuple:
    if pd.isna(spread):
        return "MA_GAP_UNKNOWN", "TARGETDAY_MA_DISTANCE_UNKNOWN_均线距离缺失"
    if spread <= 0.03:
        return "G1_3%以内", "TARGETDAY_MA_DISTANCE_G1_最大间隔3%以内_高度粘合"
    if spread <= 0.06:
        return "G2_3%-6%", "TARGETDAY_MA_DISTANCE_G2_最大间隔3%-6%_正常靠近"
    return "G3_6%以上", "TARGETDAY_MA_DISTANCE_G3_最大间隔6%以上_发散较大"


def close_ma5_distance_grade(close_value: float, ma5_value: float) -> tuple:
    if pd.isna(close_value) or pd.isna(ma5_value) or ma5_value == 0:
        return np.nan, "MA5_DISTANCE_UNKNOWN", "TARGETDAY_CLOSE_MA5_DISTANCE_UNKNOWN_距离MA5缺失"
    distance = safe_ratio(close_value, ma5_value) - 1
    if distance <= 0.05:
        return distance, "G1_小于等于5%", "TARGETDAY_CLOSE_MA5_DISTANCE_G1_收盘价距MA5小于等于5%"
    return distance, "G2_大于5%", "TARGETDAY_CLOSE_MA5_DISTANCE_G2_收盘价距MA5大于5%_偏离较大"


def targetday_new_high_30d_remark(row: pd.Series) -> str:
    close_high = row.get("is_30d_close_high")
    intraday_high = row.get("is_30d_intraday_high")
    if pd.isna(close_high) or pd.isna(intraday_high):
        return "TARGETDAY_30D_NEW_HIGH_UNKNOWN_历史不足或数据缺失"
    close_high = bool(close_high)
    intraday_high = bool(intraday_high)
    if close_high and intraday_high:
        return "TARGETDAY_30D_NEW_HIGH_CLOSE_AND_INTRADAY_收盘与盘中均创30日新高"
    if close_high:
        return "TARGETDAY_30D_NEW_HIGH_CLOSE_收盘创30日新高"
    if intraday_high:
        return "TARGETDAY_30D_NEW_HIGH_INTRADAY_ONLY_盘中创30日新高但收盘未创"
    return "TARGETDAY_NOT_30D_NEW_HIGH_未创30日新高"


def join_remarks(*parts) -> str:
    out = []
    seen = set()
    for part in parts:
        if part is None:
            continue
        if isinstance(part, float) and pd.isna(part):
            continue
        text = str(part).strip()
        if not text or text.lower() == "nan":
            continue
        for piece in text.split(";"):
            piece = piece.strip()
            if piece and piece not in seen:
                out.append(piece)
                seen.add(piece)
    return "; ".join(out)


# =========================
# 股票池过滤
# =========================
def load_st_codes() -> set:
    df = read_csv_safely(ST_FILE)
    if "ticker" not in df.columns:
        raise ValueError(f"ST 文件里没有 ticker 列，实际列名: {list(df.columns)}")
    return set(df["ticker"].astype(str).str.strip().str.zfill(6).dropna().tolist())


def load_below_8b_codes() -> set:
    df = read_csv_safely(BELOW_8B_FILE)
    if "ticker" not in df.columns:
        raise ValueError(f"80亿以下文件里没有 ticker 列，实际列名: {list(df.columns)}")
    return set(df["ticker"].astype(str).str.strip().str.zfill(6).dropna().tolist())


def load_universe_from_csv():
    df = read_csv_safely(CODE_FILE)
    required_cols = ["ticker", "secShortName"]
    for c in required_cols:
        if c not in df.columns:
            raise ValueError(f"代码文件缺少列: {c}")

    out = df.copy()
    out["ticker"] = out["ticker"].astype(str).map(normalize_to_6digits)
    out["name"] = out["secShortName"].astype(str).str.strip()
    out = out[out["ticker"].str.len() == 6].copy()

    st_codes = maybe_load_codes_from_csv(ST_FILE)
    below_8b_codes = maybe_load_codes_from_csv(BELOW_8B_FILE)

    out["is_st"] = out["ticker"].isin(st_codes)
    out["is_below_8b"] = out["ticker"].isin(below_8b_codes)

    filtered_out = out[(out["is_st"]) | (out["is_below_8b"])].copy()
    universe = out[(~out["is_st"]) & (~out["is_below_8b"])].copy()

    out["ts_code"] = out["ticker"].map(code6_to_ts_code)
    universe["ts_code"] = universe["ticker"].map(code6_to_ts_code)
    filtered_out["ts_code"] = filtered_out["ticker"].map(code6_to_ts_code)

    universe = universe.drop_duplicates("ticker").reset_index(drop=True)
    filtered_out = filtered_out.drop_duplicates("ticker").reset_index(drop=True)

    if TEST_LIMIT is not None:
        universe = universe.head(TEST_LIMIT).copy()
    else:
        universe = universe.iloc[BATCH_START:BATCH_START + BATCH_SIZE].copy()

    return universe[["ticker", "ts_code", "name"]].reset_index(drop=True), filtered_out


def file_exists(path: str) -> bool:
    return bool(path) and os.path.exists(path) and os.path.isfile(path)


def maybe_load_codes_from_csv(path: str) -> set:
    if not file_exists(path):
        return set()
    df = read_csv_safely(path)
    if "ticker" not in df.columns:
        raise ValueError(f"ticker column missing in {path}: {list(df.columns)}")
    return set(df["ticker"].astype(str).str.strip().str.zfill(6).dropna().tolist())


def get_latest_trade_date(pro) -> str:
    end_date = shanghai_today_str()
    start_date = pd.to_datetime(end_date, format="%Y%m%d") - pd.Timedelta(days=14)
    cal = call_tushare_with_retry(
        pro.trade_cal,
        max_retry=3,
        exchange="SSE",
        start_date=start_date.strftime("%Y%m%d"),
        end_date=end_date,
    )
    if cal is None or cal.empty:
        raise RuntimeError("failed to fetch trade calendar")
    cal = cal.copy()
    cal["is_open"] = pd.to_numeric(cal["is_open"], errors="coerce")
    open_days = cal[cal["is_open"] == 1]["cal_date"].astype(str).tolist()
    if not open_days:
        raise RuntimeError("no open trade day found in recent trade calendar")
    return max(open_days)


def initialize_runtime_dates(pro) -> None:
    global TARGET_DATES, TARGET_DATES_TAG
    global OBSERVATION_FILE, DEBUG_FILE, FAILED_FILE, FILTERED_FILE, OBSERVATION_LITE_FILE
    global SCAN_END_DATE, END_DATE, PRICE_ADJ_ANCHOR_DATE, FETCH_END_DATE

    target_dates_input = TARGET_DATES_INPUT
    if not target_dates_input and not TARGET_DATE_RANGES:
        target_dates_input = [get_latest_trade_date(pro)]

    TARGET_DATES = normalize_target_dates(target_dates_input, TARGET_DATE_RANGES)
    TARGET_DATES_TAG = f"{TARGET_DATES[0]}-{TARGET_DATES[-1]}" if len(TARGET_DATES) > 1 else TARGET_DATES[0]
    OBSERVATION_FILE = os.path.join(OUTPUT_DIR, f"five_buddha_pool_qfq_simple_swl1_{TARGET_DATES_TAG}.csv")
    DEBUG_FILE = os.path.join(OUTPUT_DIR, f"five_buddha_debug_rejected_qfq_simple_swl1_{TARGET_DATES_TAG}.csv")
    FAILED_FILE = os.path.join(OUTPUT_DIR, f"five_buddha_failed_fetch_qfq_simple_swl1_{TARGET_DATES_TAG}.csv")
    FILTERED_FILE = os.path.join(OUTPUT_DIR, f"five_buddha_filtered_out_qfq_simple_swl1_{TARGET_DATES_TAG}.csv")
    OBSERVATION_LITE_FILE = os.path.join(OUTPUT_DIR, f"five_buddha_pool_qfq_simple_swl1_{TARGET_DATES_TAG}_lite.csv")
    SCAN_END_DATE = max(TARGET_DATES)
    END_DATE = SCAN_END_DATE
    PRICE_ADJ_ANCHOR_DATE = (PRICE_ADJ_ANCHOR_DATE or shanghai_today_str()).replace("-", "").replace("/", "")
    FETCH_END_DATE = max(SCAN_END_DATE, PRICE_ADJ_ANCHOR_DATE)


def load_universe_from_tushare(pro):
    stock_basic = call_tushare_with_retry(
        pro.stock_basic,
        max_retry=3,
        exchange="",
        list_status="L",
        fields="ts_code,symbol,name",
    )
    if stock_basic is None or stock_basic.empty:
        raise RuntimeError("failed to fetch stock_basic")

    out = stock_basic.copy()
    out["ticker"] = out["symbol"].astype(str).map(normalize_to_6digits)
    out["name"] = out["name"].astype(str).str.strip()
    out["ts_code"] = out["ts_code"].astype(str).str.strip()
    out = out[out["ticker"].str.len() == 6].copy()

    latest_trade_date = get_latest_trade_date(pro)
    daily_basic = call_tushare_with_retry(
        pro.daily_basic,
        max_retry=3,
        trade_date=latest_trade_date,
        fields="ts_code,total_mv",
    )
    if daily_basic is None or daily_basic.empty:
        daily_basic = pd.DataFrame(columns=["ts_code", "total_mv"])

    out = out.merge(daily_basic, on="ts_code", how="left")
    out["total_mv"] = pd.to_numeric(out["total_mv"], errors="coerce")
    out["is_st"] = out["name"].str.contains("ST", case=False, na=False)
    out["is_below_8b"] = out["total_mv"].fillna(0) < 800000
    out["universe_source"] = f"tushare_auto_{latest_trade_date}"

    filtered_out = out[(out["is_st"]) | (out["is_below_8b"])].copy()
    universe = out[(~out["is_st"]) & (~out["is_below_8b"])].copy()
    return universe, filtered_out


def load_universe(pro):
    if file_exists(CODE_FILE):
        universe, filtered_out = load_universe_from_csv()
        universe["universe_source"] = "csv"
        filtered_out["universe_source"] = "csv"
    else:
        universe, filtered_out = load_universe_from_tushare(pro)

    universe = universe.drop_duplicates("ticker").reset_index(drop=True)
    filtered_out = filtered_out.drop_duplicates("ticker").reset_index(drop=True)

    if TEST_LIMIT is not None:
        universe = universe.head(TEST_LIMIT).copy()
    else:
        universe = universe.iloc[BATCH_START:BATCH_START + BATCH_SIZE].copy()

    keep_cols = [c for c in ["ticker", "ts_code", "name", "universe_source"] if c in universe.columns]
    return universe[keep_cols].reset_index(drop=True), filtered_out.reset_index(drop=True)


def call_tushare_with_retry(api_func, max_retry: int = 3, **kwargs):
    last_err = None
    for attempt in range(max_retry):
        try:
            return api_func(**kwargs)
        except Exception as e:
            last_err = e
            time.sleep(0.8 + attempt * 0.8)
    if last_err is not None:
        raise last_err
    return None


def fetch_sw_l1_classify(pro) -> pd.DataFrame:
    last_err = None
    for src in SW_INDUSTRY_SRC_CANDIDATES:
        try:
            df = call_tushare_with_retry(
                pro.index_classify,
                max_retry=3,
                level=SW_INDUSTRY_LEVEL,
                src=src,
            )
            if df is None or df.empty:
                continue
            out = df.copy()
            out["index_code"] = out["index_code"].astype(str).str.strip()
            out["industry_name"] = out["industry_name"].astype(str).str.strip()
            out["src"] = src
            return out
        except Exception as e:
            last_err = e
    if last_err is not None:
        raise RuntimeError(f"fetch_sw_l1_classify_failed: {type(last_err).__name__}: {last_err}")
    return pd.DataFrame()


def fetch_sw_index_members(pro, index_code: str) -> pd.DataFrame:
    with SW_INDEX_MEMBER_LOCK:
        cached = SW_INDEX_MEMBER_CACHE.get(index_code)
    if cached is not None:
        return cached.copy()

    df = call_tushare_with_retry(
        pro.index_member,
        max_retry=3,
        index_code=index_code,
    )
    if df is None or df.empty:
        out = pd.DataFrame(columns=["index_code", "member_ts_code", "in_date", "out_date"])
    else:
        out = df.copy()
        member_col = "con_code" if "con_code" in out.columns else "ts_code" if "ts_code" in out.columns else None
        if member_col is None:
            raise ValueError(f"index_member 返回字段中没有成分股代码列，实际列名: {list(out.columns)}")
        out["member_ts_code"] = out[member_col].astype(str).str.strip()
        if "index_code" in out.columns:
            out["index_code"] = out["index_code"].astype(str).str.strip()
        else:
            out["index_code"] = index_code
        if "in_date" in out.columns:
            out["in_date"] = out["in_date"].fillna("").astype(str).str.replace("-", "", regex=False).str.replace("/", "", regex=False)
        else:
            out["in_date"] = ""
        if "out_date" in out.columns:
            out["out_date"] = out["out_date"].fillna("").astype(str).str.replace("-", "", regex=False).str.replace("/", "", regex=False)
        else:
            out["out_date"] = ""
        out = out[["index_code", "member_ts_code", "in_date", "out_date"]].drop_duplicates().reset_index(drop=True)

    with SW_INDEX_MEMBER_LOCK:
        SW_INDEX_MEMBER_CACHE[index_code] = out.copy()
    return out.copy()


def build_stock_sw_l1_history_map(pro, universe: pd.DataFrame) -> dict:
    classify_df = fetch_sw_l1_classify(pro)
    if classify_df.empty:
        return {}

    universe_codes = set(universe["ts_code"].astype(str).str.strip().tolist())
    stock_history_map = {}

    for _, r in classify_df.iterrows():
        index_code = str(r["index_code"]).strip()
        industry_name = str(r["industry_name"]).strip()
        members = fetch_sw_index_members(pro, index_code)
        if members.empty:
            continue

        members = members[members["member_ts_code"].isin(universe_codes)].copy()
        if members.empty:
            continue

        for _, mr in members.iterrows():
            ts_code = str(mr["member_ts_code"]).strip()
            stock_history_map.setdefault(ts_code, []).append({
                "sw_l1_index_code": index_code,
                "sw_l1_name": industry_name,
                "sw_l1_in_date": str(mr.get("in_date", "") or ""),
                "sw_l1_out_date": str(mr.get("out_date", "") or ""),
            })

    return stock_history_map


def resolve_stock_sw_l1_on_date(stock_sw_histories: list, target_date: str) -> dict:
    detail = {
        "sw_l1_name": "",
        "sw_l1_index_code": "",
        "sw_l1_in_date": "",
        "sw_l1_out_date": "",
        "sw_l1_match_status": "SW_L1_NOT_FOUND",
    }
    if not stock_sw_histories:
        return detail

    target_date = str(target_date).replace("-", "").replace("/", "")
    active = []
    fallback = []
    for item in stock_sw_histories:
        in_date = str(item.get("sw_l1_in_date", "") or "")
        out_date = str(item.get("sw_l1_out_date", "") or "")
        if (not in_date or in_date <= target_date) and (not out_date or out_date >= target_date):
            active.append(item)
        fallback.append(item)

    chosen = None
    if active:
        active.sort(key=lambda x: str(x.get("sw_l1_in_date", "") or ""), reverse=True)
        chosen = active[0]
        detail["sw_l1_match_status"] = "SW_L1_MATCHED_BY_DATE"
    elif fallback:
        fallback.sort(key=lambda x: str(x.get("sw_l1_in_date", "") or ""), reverse=True)
        chosen = fallback[0]
        detail["sw_l1_match_status"] = "SW_L1_FALLBACK_LATEST"

    if chosen is None:
        return detail

    detail.update({
        "sw_l1_name": str(chosen.get("sw_l1_name", "") or ""),
        "sw_l1_index_code": str(chosen.get("sw_l1_index_code", "") or ""),
        "sw_l1_in_date": str(chosen.get("sw_l1_in_date", "") or ""),
        "sw_l1_out_date": str(chosen.get("sw_l1_out_date", "") or ""),
    })
    return detail


def fetch_sw_index_daily(pro, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    with SW_INDEX_DAILY_LOCK:
        cached = SW_INDEX_DAILY_CACHE.get(index_code)
    if cached is not None:
        return cached.copy()

    last_err = None
    raw = None
    for fetcher_name in ["sw_daily", "index_daily"]:
        fetcher = getattr(pro, fetcher_name, None)
        if fetcher is None:
            continue
        try:
            raw = call_tushare_with_retry(
                fetcher,
                max_retry=3,
                ts_code=index_code,
                start_date=start_date,
                end_date=end_date,
            )
            if raw is not None and not raw.empty:
                break
        except Exception as e:
            last_err = e

    if raw is None or raw.empty:
        if last_err is not None:
            out = pd.DataFrame(columns=["trade_date", "close", "sw_l1_ma20"])
        else:
            out = pd.DataFrame(columns=["trade_date", "close", "sw_l1_ma20"])
    else:
        out = raw.copy()
        trade_date_col = "trade_date" if "trade_date" in out.columns else "date" if "date" in out.columns else None
        if trade_date_col is None or "close" not in out.columns:
            raise ValueError(f"行业指数日线缺少字段，实际列名: {list(out.columns)}")
        out["trade_date"] = out[trade_date_col].astype(str).str.replace("-", "", regex=False).str.replace("/", "", regex=False)
        out["close"] = pd.to_numeric(out["close"], errors="coerce")
        out = out.dropna(subset=["trade_date", "close"]).copy()
        out = out.sort_values("trade_date").reset_index(drop=True)
        out["sw_l1_ma20"] = out["close"].rolling(SW_INDUSTRY_MA_N).mean()

    with SW_INDEX_DAILY_LOCK:
        SW_INDEX_DAILY_CACHE[index_code] = out.copy()
    return out.copy()


def build_sw_l1_snapshot_detail(pro, stock_sw_histories: list, target_date: str) -> dict:
    detail = resolve_stock_sw_l1_on_date(stock_sw_histories, target_date)
    detail.update({
        "sw_l1_signal_date": "",
        "sw_l1_close": np.nan,
        "sw_l1_ma20": np.nan,
        "sw_l1_above_ma20": np.nan,
        "sw_l1_above_ma20_remark": "",
    })

    index_code = detail.get("sw_l1_index_code", "")
    if not index_code:
        detail["sw_l1_above_ma20_remark"] = detail.get("sw_l1_match_status", "SW_L1_NOT_FOUND")
        return detail

    daily = fetch_sw_index_daily(pro, index_code, START_DATE, FETCH_END_DATE)
    if daily.empty:
        detail["sw_l1_above_ma20_remark"] = "SW_L1_DAILY_EMPTY"
        return detail

    target_date = str(target_date).replace("-", "").replace("/", "")
    row = daily[daily["trade_date"] == target_date]
    if row.empty:
        detail["sw_l1_above_ma20_remark"] = "SW_L1_TARGET_DATE_NO_DAILY"
        return detail

    row = row.iloc[-1]
    close_val = row.get("close", np.nan)
    ma20_val = row.get("sw_l1_ma20", np.nan)
    detail["sw_l1_signal_date"] = target_date
    detail["sw_l1_close"] = round(float(close_val), 2) if pd.notna(close_val) else np.nan
    detail["sw_l1_ma20"] = round(float(ma20_val), 2) if pd.notna(ma20_val) else np.nan

    if pd.isna(close_val) or pd.isna(ma20_val):
        detail["sw_l1_above_ma20_remark"] = "SW_L1_MA20_NOT_READY"
        return detail

    detail["sw_l1_above_ma20"] = bool(float(close_val) >= float(ma20_val))
    detail["sw_l1_above_ma20_remark"] = "SW_L1_ABOVE_MA20" if detail["sw_l1_above_ma20"] else "SW_L1_BELOW_MA20"
    return detail


# =========================
# Tushare 前复权取数：稳定版
# =========================
def fetch_tushare_bar_with_retry(pro, ts_code: str, start_date: str, end_date: str, max_retry: int = 3):
    """
    稳定取数版：沿用之前稳定的 pro.daily，再用 pro.adj_factor 手动计算前复权/后复权。

    为什么不用 ts.pro_bar？
    - pro_bar 内部会调用 tushare.pro.data_pro.py 里的 fillna(method='bfill')，
      在新版 pandas 下会刷 FutureWarning。
    - 这里直接调用 pro.daily + pro.adj_factor，不走 pro_bar，因此尽量保持之前脚本的稳定运行风格。

    复权公式：
    - qfq: adjusted_price = raw_price * adj_factor / latest_adj_factor
    - hfq: adjusted_price = raw_price * adj_factor
    - None/"none": 不复权，保留原始价格

    注意：成交量 volume 不复权，成交额 amount 保留原始口径。
    """
    last_err = None
    for attempt in range(max_retry):
        try:
            daily = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if daily is None or daily.empty:
                last_err = "daily empty dataframe"
                time.sleep(0.8 + attempt * 0.8)
                continue

            daily_basic = pro.daily_basic(
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
                fields="ts_code,trade_date,turnover_rate"
            )
            daily = daily.copy()
            daily["trade_date"] = daily["trade_date"].astype(str)
            if daily_basic is not None and not daily_basic.empty:
                daily_basic = daily_basic.copy()
                daily_basic["trade_date"] = daily_basic["trade_date"].astype(str)
                daily_basic = daily_basic[["trade_date", "turnover_rate"]].drop_duplicates("trade_date")
                daily_basic["turnover_rate"] = pd.to_numeric(daily_basic["turnover_rate"], errors="coerce")
                daily = daily.merge(daily_basic, on="trade_date", how="left")
            else:
                daily["turnover_rate"] = np.nan

            mode = str(PRICE_ADJ_MODE).lower() if PRICE_ADJ_MODE is not None else "none"
            if mode in ["", "none", "nan"]:
                daily["adj_factor"] = 1.0
                return daily

            adj = pro.adj_factor(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if adj is None or adj.empty:
                # 如果adj_factor暂时取不到，不直接报错；返回原始daily，并打上未复权标记，后续标准化时会记录。
                daily = daily.copy()
                daily["adj_factor"] = np.nan
                daily["adj_factor_missing"] = True
                return daily
            adj = adj.copy()
            adj["trade_date"] = adj["trade_date"].astype(str)
            adj = adj[["trade_date", "adj_factor"]].drop_duplicates("trade_date")
            adj["adj_factor"] = pd.to_numeric(adj["adj_factor"], errors="coerce")

            merged = daily.merge(adj, on="trade_date", how="left")
            # 不用 fillna(method=...)，避免 pandas FutureWarning
            merged = merged.sort_values("trade_date").reset_index(drop=True)
            merged["adj_factor"] = merged["adj_factor"].bfill().ffill()
            merged["adj_factor_missing"] = merged["adj_factor"].isna()
            return merged

        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(0.8 + attempt * 0.8)

    raise RuntimeError(str(last_err))


def standardize_tushare_bar(df: pd.DataFrame) -> pd.DataFrame:
    """标准化行情，并按 PRICE_ADJ_MODE 生成前复权/后复权价格。"""
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    required_cols = ["trade_date", "open", "high", "low", "close", "vol"]
    missing = [c for c in required_cols if c not in out.columns]
    if missing:
        raise ValueError(f"Tushare daily 缺少字段: {missing}")

    out["date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce")
    for c in ["open", "high", "low", "close", "vol"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out["volume"] = out["vol"]
    out["amount"] = pd.to_numeric(out["amount"], errors="coerce") if "amount" in out.columns else np.nan
    out["turnover_rate"] = pd.to_numeric(out["turnover_rate"], errors="coerce") if "turnover_rate" in out.columns else np.nan

    mode = str(PRICE_ADJ_MODE).lower() if PRICE_ADJ_MODE is not None else "none"
    if mode not in ["", "none", "nan"]:
        if "adj_factor" not in out.columns:
            raise ValueError("缺少 adj_factor，无法执行前复权/后复权")
        out["adj_factor"] = pd.to_numeric(out["adj_factor"], errors="coerce")
        out = out.sort_values("date").reset_index(drop=True)
        out["adj_factor"] = out["adj_factor"].bfill().ffill()

        if out["adj_factor"].isna().all():
            raise ValueError("adj_factor 全部缺失，无法执行前复权/后复权")

        if mode == "qfq":
            latest_adj = float(out["adj_factor"].dropna().iloc[-1])
            if latest_adj == 0:
                raise ValueError("latest_adj_factor 为0，无法计算前复权")
            adj_multiplier = out["adj_factor"] / latest_adj
        elif mode == "hfq":
            adj_multiplier = out["adj_factor"]
        else:
            raise ValueError(f"PRICE_ADJ_MODE 只支持 qfq/hfq/None，当前为: {PRICE_ADJ_MODE}")

        for c in ["open", "high", "low", "close"]:
            out[c] = out[c] * adj_multiplier

    # 复权后重新计算pct_chg，避免除权日被原始价格口径污染。
    out = out.dropna(subset=["date", "open", "high", "low", "close", "volume"]).copy()
    out = out.sort_values("date").reset_index(drop=True)
    out["pct_chg"] = out["close"].pct_change()

    return out[["date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover_rate"]]


# =========================
# 指标构建
# =========================
def build_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        return out

    if "pct_chg" not in out.columns or out["pct_chg"].isna().all():
        out["pct_chg"] = out["close"].pct_change()

    for n in [5, 10, 20, 30, 60, 120, 240, 250]:
        out[f"ma{n}"] = out["close"].rolling(n).mean()

    for n in [5, 10, 20]:
        out[f"vol_ma{n}"] = out["volume"].rolling(n).mean()

    for n in [5, 10, 20, 30, 60]:
        out[f"ma{n}_slope"] = out[f"ma{n}"] - out[f"ma{n}"].shift(1)

    out["bar_range_abs"] = (out["high"] - out["low"]).clip(lower=1e-8)
    out["body"] = (out["close"] - out["open"]).abs()
    out["body_ratio"] = out["body"] / out["bar_range_abs"]
    out["close_near_high"] = (out["high"] - out["close"]) / out["bar_range_abs"]
    out["range_pct"] = (out["high"] - out["low"]) / out["close"].replace(0, np.nan)
    out["five_line_spread"] = out.apply(five_line_spread, axis=1)

    out["close_30d_high"] = out["close"].rolling(FB_NEW_HIGH_LOOKBACK, min_periods=FB_NEW_HIGH_LOOKBACK).max()
    out["high_30d_high"] = out["high"].rolling(FB_NEW_HIGH_LOOKBACK, min_periods=FB_NEW_HIGH_LOOKBACK).max()
    out["is_30d_close_high"] = np.where(out["close_30d_high"].notna(), out["close"] >= out["close_30d_high"], np.nan)
    out["is_30d_intraday_high"] = np.where(out["high_30d_high"].notna(), out["high"] >= out["high_30d_high"], np.nan)

    return out


def slice_df_to_target_date(df: pd.DataFrame, target_date: str) -> pd.DataFrame:
    target_dt = to_target_dt(target_date)
    sub = df[df["date"] <= target_dt].copy()
    sub = sub.sort_values("date").reset_index(drop=True)
    return sub


# =========================
# 五佛手硬规则识别
# =========================
def five_buddha_scored_detail(df: pd.DataFrame, idx: int) -> dict:
    row = df.iloc[idx]
    out = {"fb_candidate": False, "fb_hard_fail_reason": ""}
    need_cols = ["ma5", "ma10", "ma20", "ma30", "ma60", "ma250"]

    if any(pd.isna(row[c]) for c in need_cols):
        out["fb_hard_fail_reason"] = "MA_MISSING"
        return out

    # 硬条件：收盘价必须站上全部五条均线
    if FB_REQUIRE_CLOSE_ABOVE_ALL_MA:
        below_mas = [c.upper() for c in need_cols if row["close"] < row[c]]
        if below_mas:
            out["fb_hard_fail_reason"] = "CLOSE_BELOW_" + "_".join(below_mas)
            out["fb_below_ma_list"] = ",".join(below_mas)
            return out

    ma_detail = ma_structure_detail(row)
    out.update(ma_detail)
    if FB_REQUIRE_MA30_MA60_MA250_BELOW_SHORT_MAS and not ma_detail.get("ma_structure_ok", False):
        out["fb_hard_fail_reason"] = ma_detail.get("ma_structure_reason", "MA_STRUCTURE_FAIL")
        return out

    recent_min, recent_min_date, recent_min_lowest_ma_name = recent_min_spread(df, idx, FB_LOOKBACK)
    current_spread = row["five_line_spread"]
    current_lowest_ma_name = min_ma_name_on_row(row)
    recent_converge_limit = (
        FB_RECENT_CONVERGE_MAX_IF_MIN_IS_MA60
        if recent_min_lowest_ma_name == "MA60"
        else FB_RECENT_CONVERGE_MAX
    )
    current_spread_limit = (
        FB_CURRENT_SPREAD_MAX_IF_MIN_IS_MA60
        if current_lowest_ma_name == "MA60"
        else FB_CURRENT_SPREAD_MAX
    )
    close_over_ma20_limit = (
        FB_MAX_CLOSE_OVER_MA20_IF_MIN_IS_MA60
        if current_lowest_ma_name == "MA60"
        else FB_MAX_CLOSE_OVER_MA20
    )

    if pd.isna(recent_min) or recent_min > recent_converge_limit:
        out["fb_hard_fail_reason"] = "NO_RECENT_CONVERGENCE"
        out["fb_recent_min_5line_spread_pct"] = round(float(recent_min) * 100, 2) if pd.notna(recent_min) else np.nan
        out["fb_recent_min_5line_spread_date"] = recent_min_date
        out["fb_recent_min_5line_lowest_ma"] = recent_min_lowest_ma_name
        out["fb_recent_converge_limit_pct"] = round(float(recent_converge_limit) * 100, 2)
        return out

    if pd.isna(current_spread) or current_spread > current_spread_limit:
        out["fb_hard_fail_reason"] = "CURRENT_SPREAD_TOO_WIDE"
        out["fb_current_5line_spread_pct"] = round(float(current_spread) * 100, 2) if pd.notna(current_spread) else np.nan
        out["fb_current_5line_lowest_ma"] = current_lowest_ma_name
        out["fb_current_spread_limit_pct"] = round(float(current_spread_limit) * 100, 2)
        return out

    over_ma20 = safe_ratio(row["close"], row["ma20"]) - 1
    if pd.notna(over_ma20) and over_ma20 > close_over_ma20_limit:
        out["fb_hard_fail_reason"] = "PRICE_TOO_FAR_FROM_MA20"
        out["fb_current_5line_lowest_ma"] = current_lowest_ma_name
        out["fb_close_over_ma20_pct"] = round(float(over_ma20) * 100, 2) if pd.notna(over_ma20) else np.nan
        out["fb_close_over_ma20_limit_pct"] = round(float(close_over_ma20_limit) * 100, 2)
        return out

    # MA30/MA60不能快速下压
    ma30_3d = calc_change_vs_self(row["ma30"], df.iloc[idx - 3]["ma30"]) if idx >= 3 else np.nan
    ma60_5d = calc_change_vs_self(row["ma60"], df.iloc[idx - 5]["ma60"]) if idx >= 5 else np.nan
    if pd.isna(ma30_3d) or ma30_3d < FB_MA30_3D_FLOOR:
        out["fb_hard_fail_reason"] = "MA30_FAST_DOWN"
        return out
    if pd.isna(ma60_5d) or ma60_5d < FB_MA60_5D_FLOOR:
        out["fb_hard_fail_reason"] = "MA60_FAST_DOWN"
        return out
        
    if FB_REQUIRE_LAST3_VOL_ABOVE_MA20:
        if idx < 2:
            out["fb_hard_fail_reason"] = "LAST3_VOL_MA20_LOOKBACK_NOT_ENOUGH"
            return out
        vol_win = df.iloc[idx - 2: idx + 1][["volume", "vol_ma20"]].copy()
        if vol_win.isna().any().any():
            out["fb_hard_fail_reason"] = "LAST3_VOL_MA20_MISSING"
            return out
    
        last3_vol_above_ma20_count = int((vol_win["volume"] >= vol_win["vol_ma20"]).sum())
        if last3_vol_above_ma20_count < 2:
            out["fb_hard_fail_reason"] = "LAST3_VOLUME_LT_2_DAYS_ABOVE_MA20"
            out["fb_last3_vol_above_ma20_count"] = last3_vol_above_ma20_count
            return out
    
        out["fb_last3_vol_above_ma20"] = True
        out["fb_last3_vol_above_ma20_count"] = last3_vol_above_ma20_count


    



    
    if idx < 1:
        out["fb_hard_fail_reason"] = "TURNOVER_RATE_LOOKBACK_NOT_ENOUGH"
        return out

    prev_row = df.iloc[idx - 1]
    current_turnover = row.get("turnover_rate", np.nan)
    prev_turnover = prev_row.get("turnover_rate", np.nan)
    if pd.isna(current_turnover) or pd.isna(prev_turnover):
        out["fb_hard_fail_reason"] = "TURNOVER_RATE_MISSING"
        return out

    if float(current_turnover) < FB_TURNOVER_RATE_MIN and float(prev_turnover) < FB_TURNOVER_RATE_MIN:
        out["fb_hard_fail_reason"] = "TURNOVER_RATE_LAST2_BOTH_BELOW_1P5"
        return out

    is_30d_close_high = row.get("is_30d_close_high", np.nan)
    is_30d_intraday_high = row.get("is_30d_intraday_high", np.nan)
    if pd.isna(is_30d_close_high) or pd.isna(is_30d_intraday_high):
        out["fb_hard_fail_reason"] = "NEW_HIGH_30D_MISSING"
        return out

    if not (bool(is_30d_close_high) or bool(is_30d_intraday_high)):
        out["fb_hard_fail_reason"] = "NOT_30D_NEW_HIGH"
        return out

    close_over_ma10 = safe_ratio(row["close"], row["ma10"]) - 1
    close_over_ma5 = safe_ratio(row["close"], row["ma5"]) - 1
    close_over_ma30 = safe_ratio(row["close"], row["ma30"]) - 1
    close_over_ma60 = safe_ratio(row["close"], row["ma60"]) - 1

    out.update({
        "fb_candidate": True,
        "fb_hard_fail_reason": "",
        "fb_candidate_reason": "FB_HARD_RULES_PASSED",
        "fb_recent_min_5line_spread_pct": round(float(recent_min) * 100, 2),
        "fb_recent_min_5line_spread_date": recent_min_date,
        "fb_recent_min_5line_lowest_ma": recent_min_lowest_ma_name,
        "fb_recent_converge_limit_pct": round(float(recent_converge_limit) * 100, 2),
        "fb_current_5line_spread_pct": round(float(current_spread) * 100, 2),
        "fb_current_5line_lowest_ma": current_lowest_ma_name,
        "fb_current_spread_limit_pct": round(float(current_spread_limit) * 100, 2),
        "fb_ma30_3d_change_pct": round(float(ma30_3d) * 100, 2) if pd.notna(ma30_3d) else np.nan,
        "fb_ma60_5d_change_pct": round(float(ma60_5d) * 100, 2) if pd.notna(ma60_5d) else np.nan,
        "fb_close_over_ma20_pct": round(float(over_ma20) * 100, 2) if pd.notna(over_ma20) else np.nan,
        "fb_close_over_ma20_limit_pct": round(float(close_over_ma20_limit) * 100, 2),
        "fb_close_over_ma10_pct": round(float(close_over_ma10) * 100, 2) if pd.notna(close_over_ma10) else np.nan,
        "fb_close_over_ma5_pct": round(float(close_over_ma5) * 100, 2) if pd.notna(close_over_ma5) else np.nan,
        "fb_close_over_ma30_pct": round(float(close_over_ma30) * 100, 2) if pd.notna(close_over_ma30) else np.nan,
        "fb_close_over_ma60_pct": round(float(close_over_ma60) * 100, 2) if pd.notna(close_over_ma60) else np.nan,
        "fb_last3_vol_above_ma20": bool(FB_REQUIRE_LAST3_VOL_ABOVE_MA20),
        "fb_turnover_rate": round(float(current_turnover), 2),
        "fb_prev_turnover_rate": round(float(prev_turnover), 2),
        "fb_30d_new_high_pass": True,
    })
    return out


# =========================
# 6.3 买点确认：B2-B5 + S/A/B/C评级
# =========================
def _volume_extra_base(prefix: str, grade: str, remark: str, extra: dict) -> dict:
    out = dict(extra or {})
    out.update({
        "volume_confirm_grade": f"{prefix}_{grade}" if grade else "",
        "volume_confirm_grade_short": grade,
        "volume_confirm_rank": GRADE_RANK.get(grade, 0),
        "volume_confirm_remark": remark,
        "volume_confirm_is_formal": bool(grade in ["S", "A", "B"]),
    })
    return out


def b2_strong_up_grade(df: pd.DataFrame, idx: int):
    row = df.iloc[idx]
    need_cols = ["vol_ma5", "vol_ma10", "vol_ma20", "close_near_high", "pct_chg", "ma5"]
    if any(pd.isna(row[c]) for c in need_cols):
        return False, "", {}

    prev_vol = df.iloc[idx - 1]["volume"] if idx >= 1 else np.nan
    extra = {
        "b2_pct_chg_pct": round(float(row["pct_chg"]) * 100, 2),
        "b2_vol_vs_ma5": round(safe_ratio(row["volume"], row["vol_ma5"]), 2),
        "b2_vol_vs_ma10": round(safe_ratio(row["volume"], row["vol_ma10"]), 2),
        "b2_vol_vs_ma20": round(safe_ratio(row["volume"], row["vol_ma20"]), 2),
    }

    if (row["pct_chg"] >= 0.05 and row["volume"] > row["vol_ma10"] and row["volume"] > row["vol_ma20"] and
            row["close_near_high"] <= 0.15 and row["close"] >= row["ma5"]):
        return True, "S", _volume_extra_base("B2", "S", "B2_S_强势放量上涨_涨幅5%以上且量超10日和20日均量", extra)

    if (row["pct_chg"] >= B2_STRONG_UP_PCT and row["volume"] > row["vol_ma5"] and row["volume"] > row["vol_ma20"] and
            row["close_near_high"] <= B2_CLOSE_NEAR_HIGH_MAX and row["close"] >= row["ma5"]):
        return True, "A", _volume_extra_base("B2", "A", "B2_A_放量上涨_沿用原B2标准", extra)

    if (row["pct_chg"] >= 0.03 and (row["volume"] > row["vol_ma10"] or row["volume"] > row["vol_ma20"]) and
            row["close_near_high"] <= 0.35 and row["close"] >= row["ma5"]):
        return True, "B", _volume_extra_base("B2", "B", "B2_B_温和放量上涨_涨幅3%以上且至少超过一条量均线", extra)

    if (row["pct_chg"] > 0 and pd.notna(prev_vol) and row["volume"] > prev_vol * 1.20 and
            row["close_near_high"] <= 0.40 and row["close"] >= row["ma5"]):
        return True, "C", _volume_extra_base("B2", "C", "B2_C_当日上涨且较前一日明显放量_观察确认", extra)

    return False, "", {}


def b3_continuous_up_grade(df: pd.DataFrame, idx: int):
    if idx < 2:
        return False, "", {}
    win = df.iloc[idx - 2: idx + 1].copy()
    row = df.iloc[idx]

    required = ["close", "pct_chg", "volume", "vol_ma5", "vol_ma10", "vol_ma20"]
    if win[required].isna().any().any() or pd.isna(row["ma5"]):
        return False, "", {}

    up_days = int((win["pct_chg"] > 0).sum())
    all_up = up_days == 3
    vol_increasing_all = bool(win["volume"].iloc[0] < win["volume"].iloc[1] < win["volume"].iloc[2])
    vol_above_10_20_days = int(((win["volume"] > win["vol_ma10"]) & (win["volume"] > win["vol_ma20"])).sum())
    vol_above_10_or_20_days = int(((win["volume"] > win["vol_ma10"]) | (win["volume"] > win["vol_ma20"])).sum())
    target_vol_gt_ma5_ma20 = bool(row["volume"] > row["vol_ma5"] and row["volume"] > row["vol_ma20"])
    target_vol_gt_ma10 = bool(row["volume"] > row["vol_ma10"])
    overall_up = float(win.iloc[-1]["close"]) > float(win.iloc[0]["close"])
    no_big_bear = float(win["pct_chg"].min()) > B3_DAILY_DROP_FLOOR
    total_up = float(win.iloc[-1]["close"]) / float(win.iloc[0]["close"]) - 1.0

    extra = {
        "b3_up_days": up_days,
        "b3_all_up": all_up,
        "b3_volume_increasing_all": vol_increasing_all,
        "b3_vol_above_10_20_days": vol_above_10_20_days,
        "b3_vol_above_10_or_20_days": vol_above_10_or_20_days,
        "b3_target_vol_gt_ma5_ma20": target_vol_gt_ma5_ma20,
        "b3_3d_total_up_pct": round(total_up * 100, 2),
        "b3_min_day_pct": round(float(win["pct_chg"].min()) * 100, 2),
    }

    # S：用户提出的最紧版本 + 至少2天量能强于10/20日均量
    if (all_up and vol_increasing_all and vol_above_10_20_days >= 2 and target_vol_gt_ma5_ma20 and
            row["close"] >= row["ma5"] and overall_up and no_big_bear):
        return True, "S", _volume_extra_base("B3", "S", "B3_S_三天持续上涨_成交量持续放大_目标日量超5日和20日均量_无大阴跌", extra)

    # A：你刚改过的口径：最近3天至少2天量同时大于10日均量和20日均量
    if (up_days >= 2 and vol_above_10_20_days >= 2 and target_vol_gt_ma5_ma20 and
            row["close"] >= row["ma5"] and overall_up and no_big_bear):
        return True, "A", _volume_extra_base("B3", "A", "B3_A_三日内至少两日上涨且两日量超10日和20日均量_目标日继续放量", extra)

    if (up_days >= 2 and vol_above_10_or_20_days >= 2 and target_vol_gt_ma10 and
            row["close"] >= row["ma5"] and overall_up and no_big_bear):
        return True, "B", _volume_extra_base("B3", "B", "B3_B_三日内至少两日上涨且两日量超10日或20日均量_宽松确认", extra)

    if (overall_up and no_big_bear and row["pct_chg"] > 0 and row["close"] >= row["ma5"] and
            (target_vol_gt_ma10 or row["volume"] > win.iloc[-2]["volume"] or row["volume"] > row["vol_ma5"] * 0.95)):
        return True, "C", _volume_extra_base("B3", "C", "B3_C_三日整体向上且目标日量能未明显萎缩_观察确认", extra)

    return False, "", {}


def b4_slow_up_grade(df: pd.DataFrame, idx: int):
    if idx < 4:
        return False, "", {}
    win = df.iloc[idx - 4: idx + 1].copy()
    row = df.iloc[idx]

    required = ["close", "pct_chg", "volume", "vol_ma5", "vol_ma10", "vol_ma20"]
    if win[required].isna().any().any() or pd.isna(row["ma5"]):
        return False, "", {}

    total_up = float(win.iloc[-1]["close"]) / float(win.iloc[0]["close"]) - 1.0
    up_days = int((win["pct_chg"] > 0).sum())
    vol_above_20_count = int((win["volume"] > win["vol_ma20"]).sum())
    vol_above_10_or_20_count = int(((win["volume"] > win["vol_ma10"]) | (win["volume"] > win["vol_ma20"])).sum())
    vol_rising_count = int((win["volume"].diff() > 0).sum())
    min_day_pct = float(win["pct_chg"].min())
    no_big_bear = min_day_pct > B4_DAILY_DROP_FLOOR

    extra = {
        "b4_5d_total_up_pct": round(total_up * 100, 2),
        "b4_up_days": up_days,
        "b4_vol_above_20_count": vol_above_20_count,
        "b4_vol_above_10_or_20_count": vol_above_10_or_20_count,
        "b4_vol_rising_count": vol_rising_count,
        "b4_min_day_pct": round(min_day_pct * 100, 2),
    }

    if (0.03 <= total_up <= 0.08 and up_days >= 4 and vol_above_20_count >= 4 and
            row["volume"] > row["vol_ma10"] and row["close"] >= row["ma5"] and no_big_bear):
        return True, "S", _volume_extra_base("B4", "S", "B4_S_五日温和上涨_四日收涨_四日量超20日均量", extra)

    if (B4_5D_TOTAL_UP_MIN <= total_up <= B4_5D_TOTAL_UP_MAX and up_days >= 3 and vol_above_20_count >= 3 and
            row["volume"] > row["vol_ma5"] and row["close"] >= row["ma5"] and no_big_bear):
        return True, "A", _volume_extra_base("B4", "A", "B4_A_缓慢放量上涨_沿用原B4标准", extra)

    if (0.02 <= total_up <= 0.10 and up_days >= 3 and vol_above_10_or_20_count >= 3 and
            row["pct_chg"] >= 0 and row["close"] >= row["ma5"] and no_big_bear):
        return True, "B", _volume_extra_base("B4", "B", "B4_B_五日整体温和上涨_量能至少多日超过10日或20日均量", extra)

    if (total_up > 0 and up_days >= 3 and no_big_bear and row["close"] >= row["ma5"] and
            (vol_rising_count >= 2 or row["volume"] > row["vol_ma5"] * 0.95)):
        return True, "C", _volume_extra_base("B4", "C", "B4_C_五日整体向上且量能温和抬升_观察确认", extra)

    return False, "", {}


def b5_huge_up_grade(df: pd.DataFrame, idx: int):
    if idx < 1:
        return False, "", {}
    win = df.iloc[idx - 1: idx + 1].copy()
    row = df.iloc[idx]

    required = ["close", "pct_chg", "volume", "vol_ma20"]
    if win[required].isna().any().any() or pd.isna(row["close_near_high"]):
        return False, "", {}

    total_up = float(win.iloc[-1]["close"]) / float(win.iloc[0]["close"]) - 1.0
    last2_up = bool((win["pct_chg"] > 0).all())
    last2_vol_25 = bool((win["volume"] >= win["vol_ma20"] * B5_VOL_VS_MA20_STRICT).all())
    last2_vol_20 = bool((win["volume"] >= win["vol_ma20"] * B5_VOL_VS_MA20_LOOSE).all())
    target_vol_25 = bool(row["volume"] >= row["vol_ma20"] * B5_VOL_VS_MA20_STRICT)
    target_vol_20 = bool(row["volume"] >= row["vol_ma20"] * B5_VOL_VS_MA20_LOOSE)

    extra = {
        "b5_2d_total_up_pct": round(total_up * 100, 2),
        "b5_last2_up": last2_up,
        "b5_last2_vol_ge_2p5_ma20": last2_vol_25,
        "b5_last2_vol_ge_2p0_ma20": last2_vol_20,
        "b5_target_vol_vs_ma20": round(safe_ratio(row["volume"], row["vol_ma20"]), 2),
    }

    if (last2_vol_25 and last2_up and total_up >= B5_2D_TOTAL_UP_STRICT and row["close_near_high"] <= B5_CLOSE_NEAR_HIGH_MAX):
        return True, "S", _volume_extra_base("B5", "S", "B5_S_连续两日巨量上涨_量均超20日均量2.5倍", extra)

    if (last2_vol_20 and total_up >= B5_2D_TOTAL_UP_LOOSE and row["pct_chg"] >= 0 and row["close_near_high"] <= B5_CLOSE_NEAR_HIGH_MAX):
        return True, "A", _volume_extra_base("B5", "A", "B5_A_连续两日放巨量上涨_量均超20日均量2倍", extra)

    if target_vol_25 and row["pct_chg"] >= B2_STRONG_UP_PCT and row["close_near_high"] <= B5_CLOSE_NEAR_HIGH_MAX:
        return True, "B", _volume_extra_base("B5", "B", "B5_B_目标日单日巨量强涨_等待连续性确认", extra)

    if target_vol_20 and row["pct_chg"] > 0:
        return True, "C", _volume_extra_base("B5", "C", "B5_C_目标日单日放巨量上涨_观察确认", extra)

    return False, "", {}


BUY_POINT_TYPE_PRIORITY = {
    "B5_持续巨量上涨": 40,
    "B2_放量上涨": 30,
    "B3_持续放量上涨": 20,
    "B4_缓慢放量上涨": 10,
}


def b2_book_strong_up_grade(df: pd.DataFrame, idx: int):
    if idx < 1:
        return False, "", {}

    win = df.iloc[idx - 1: idx + 1].copy()
    row = win.iloc[-1]
    prev = win.iloc[0]
    required = ["open", "close", "pct_chg", "volume", "vol_ma5", "vol_ma10", "vol_ma20", "close_near_high"]
    if not has_required_columns(win, required):
        return False, "", {}

    prev_up = bool(prev["pct_chg"] > 0)
    row_up = bool(row["pct_chg"] > 0)
    prev_bull = is_bullish_bar(prev)
    row_bull = is_bullish_bar(row)
    overall_up = bool(row["close"] > prev["close"])
    current_vol_gt_prev = bool(row["volume"] > prev["volume"])
    current_vol_ratio_vs_prev = safe_ratio(row["volume"], prev["volume"])
    current_vol_gt_ma5 = bool(row["volume"] > row["vol_ma5"])
    current_vol_gt_ma10 = bool(row["volume"] > row["vol_ma10"])
    current_vol_gt_ma20 = bool(row["volume"] > row["vol_ma20"])
    total_up = float(row["close"]) / float(prev["close"]) - 1.0

    extra = {
        "buy_point_ruleset": BUY_POINT_RULESET,
        "b2_prev_up": prev_up,
        "b2_target_up": row_up,
        "b2_prev_bull": prev_bull,
        "b2_target_bull": row_bull,
        "b2_pct_chg_pct": round(float(row["pct_chg"]) * 100, 2),
        "b2_prev_pct_chg_pct": round(float(prev["pct_chg"]) * 100, 2),
        "b2_vol_vs_prev": round(current_vol_ratio_vs_prev, 2) if pd.notna(current_vol_ratio_vs_prev) else np.nan,
        "b2_vol_vs_ma5": round(safe_ratio(row["volume"], row["vol_ma5"]), 2),
        "b2_vol_vs_ma10": round(safe_ratio(row["volume"], row["vol_ma10"]), 2),
        "b2_vol_vs_ma20": round(safe_ratio(row["volume"], row["vol_ma20"]), 2),
        "b2_2d_total_up_pct": round(total_up * 100, 2),
    }

    if (prev_up and row_up and prev_bull and row_bull and overall_up and current_vol_gt_prev and current_vol_gt_ma5 and
            current_vol_gt_ma10 and current_vol_gt_ma20 and row["pct_chg"] >= B2_STRONG_UP_PCT and total_up >= B2_STRONG_UP_PCT and
            pd.notna(current_vol_ratio_vs_prev) and current_vol_ratio_vs_prev >= B2_BOOK_VOL_RATIO_BASIC and
            row["close_near_high"] <= B2_CLOSE_NEAR_HIGH_MAX):
        return True, "A", _volume_extra_base("B2", "A", "B2_A_two_bar_strong_up_second_bar_volume_confirms_strict", extra)

    return False, "", {}


def b3_book_continuous_up_grade(df: pd.DataFrame, idx: int):
    if idx < 2:
        return False, "", {}

    win = df.iloc[idx - 2: idx + 1].copy()
    required = ["open", "close", "pct_chg", "volume", "vol_ma10", "vol_ma20", "close_near_high"]
    if not has_required_columns(win, required):
        return False, "", {}

    up_days = int((win["pct_chg"] > 0).sum())
    bullish_days = int(win.apply(is_bullish_bar, axis=1).sum())
    all_up = up_days == 3
    vol_increasing_all = bool(win["volume"].iloc[0] < win["volume"].iloc[1] < win["volume"].iloc[2])
    vol_gentle_up = bool(
        win["volume"].iloc[1] >= win["volume"].iloc[0] * B3_BOOK_VOL_EQUAL_TOL and
        win["volume"].iloc[2] >= win["volume"].iloc[1]
    )
    target_vol_is_highest = bool(win["volume"].iloc[2] >= win["volume"].iloc[:2].max())
    target_vol_gt_ma10 = bool(win["volume"].iloc[2] > win["vol_ma10"].iloc[2])
    target_vol_gt_ma20 = bool(win["volume"].iloc[2] > win["vol_ma20"].iloc[2])
    overall_up = bool(win.iloc[-1]["close"] > win.iloc[0]["close"])
    total_up = float(win.iloc[-1]["close"]) / float(win.iloc[0]["close"]) - 1.0

    extra = {
        "buy_point_ruleset": BUY_POINT_RULESET,
        "b3_up_days": up_days,
        "b3_bullish_days": bullish_days,
        "b3_all_up": all_up,
        "b3_volume_increasing_all": vol_increasing_all,
        "b3_volume_gentle_up": vol_gentle_up,
        "b3_target_vol_is_highest": target_vol_is_highest,
        "b3_target_vol_gt_ma10": target_vol_gt_ma10,
        "b3_target_vol_gt_ma20": target_vol_gt_ma20,
        "b3_3d_total_up_pct": round(total_up * 100, 2),
        "b3_first_vol": round(float(win["volume"].iloc[0]), 0),
        "b3_second_vol": round(float(win["volume"].iloc[1]), 0),
        "b3_third_vol": round(float(win["volume"].iloc[2]), 0),
    }

    if (all_up and bullish_days == 3 and overall_up and total_up >= B2_STRONG_UP_PCT and
            vol_gentle_up and target_vol_is_highest and target_vol_gt_ma10 and target_vol_gt_ma20 and
            win.iloc[-1]["close_near_high"] <= 0.35):
        return True, "A", _volume_extra_base("B3", "A", "B3_A_three_bar_continuous_up_volume_confirms_strict", extra)

    return False, "", {}


def b4_book_slow_up_grade(df: pd.DataFrame, idx: int):
    if idx < 4:
        return False, "", {}

    window_size = 5
    win = df.iloc[idx - window_size + 1: idx + 1].copy()
    required = ["open", "close", "pct_chg", "volume", "close_near_high"]
    if not has_required_columns(win, required):
        return False, "", {}

    row = win.iloc[-1]
    total_up = float(win.iloc[-1]["close"]) / float(win.iloc[0]["close"]) - 1.0
    up_days = int((win["pct_chg"] > 0).sum())
    bullish_days = int(win.apply(is_bullish_bar, axis=1).sum())
    no_big_drop = bool(float(win["pct_chg"].min()) > B4_DAILY_DROP_FLOOR)
    head_len = max(1, window_size // 2)
    tail_len = max(1, window_size - head_len)
    head_avg = avg_volume(win["volume"].iloc[:head_len])
    tail_avg = avg_volume(win["volume"].iloc[-tail_len:])
    tail_gt_head = bool(pd.notna(head_avg) and pd.notna(tail_avg) and tail_avg >= head_avg * B4_BOOK_VOL_TAIL_HEAD_RATIO)
    current_vol_not_weak = bool(row["volume"] >= win["volume"].median() * 0.95)
    overall_up = bool(win.iloc[-1]["close"] > win.iloc[0]["close"])
    target_close_is_window_high = bool(row["close"] >= win["close"].iloc[:-1].max())

    extra = {
        "buy_point_ruleset": BUY_POINT_RULESET,
        "b4_window_size": window_size,
        "b4_total_up_pct": round(total_up * 100, 2),
        "b4_up_days": up_days,
        "b4_bullish_days": bullish_days,
        "b4_no_big_drop": no_big_drop,
        "b4_target_close_is_window_high": target_close_is_window_high,
        "b4_head_avg_volume": round(head_avg, 0) if pd.notna(head_avg) else np.nan,
        "b4_tail_avg_volume": round(tail_avg, 0) if pd.notna(tail_avg) else np.nan,
        "b4_tail_head_vol_ratio": round(safe_ratio(tail_avg, head_avg), 2) if pd.notna(head_avg) and head_avg != 0 else np.nan,
    }

    if (overall_up and B4_5D_TOTAL_UP_MIN <= total_up <= B4_5D_TOTAL_UP_MAX and up_days >= 4 and bullish_days >= 4 and
            no_big_drop and tail_gt_head and current_vol_not_weak and is_bullish_bar(row) and row["pct_chg"] > 0 and
            target_close_is_window_high and row["close_near_high"] <= 0.40):
        return True, "A", _volume_extra_base("B4", "A", "B4_A_five_bar_slow_up_volume_confirms_strict", extra)

    return False, "", {}


def b5_book_huge_up_grade(df: pd.DataFrame, idx: int):
    if idx < 1:
        return False, "", {}

    win = df.iloc[idx - 1: idx + 1].copy()
    row = win.iloc[-1]
    prev = win.iloc[0]
    required = ["open", "close", "pct_chg", "volume", "vol_ma20", "close_near_high"]
    if not has_required_columns(win, required):
        return False, "", {}

    total_up = float(win.iloc[-1]["close"]) / float(win.iloc[0]["close"]) - 1.0
    last2_up = bool((win["pct_chg"] > 0).all())
    last2_bull = bool(win.apply(is_bullish_bar, axis=1).all())
    last2_vol_25 = bool((win["volume"] >= win["vol_ma20"] * B5_VOL_VS_MA20_STRICT).all())
    last2_vol_20 = bool((win["volume"] >= win["vol_ma20"] * B5_VOL_VS_MA20_LOOSE).all())
    last2_vol_16 = bool((win["volume"] >= win["vol_ma20"] * B5_BOOK_VOL_VS_MA20_BASIC).all())
    target_vol_not_shrinking = bool(row["volume"] >= prev["volume"] * 0.90)

    extra = {
        "buy_point_ruleset": BUY_POINT_RULESET,
        "b5_2d_total_up_pct": round(total_up * 100, 2),
        "b5_last2_up": last2_up,
        "b5_last2_bull": last2_bull,
        "b5_last2_vol_ge_2p5_ma20": last2_vol_25,
        "b5_last2_vol_ge_2p0_ma20": last2_vol_20,
        "b5_last2_vol_ge_1p6_ma20": last2_vol_16,
        "b5_target_vol_not_shrinking": target_vol_not_shrinking,
        "b5_target_vol_vs_ma20": round(safe_ratio(row["volume"], row["vol_ma20"]), 2),
    }

    if (last2_bull and last2_up and last2_vol_20 and target_vol_not_shrinking and total_up >= B5_2D_TOTAL_UP_STRICT and
            row["pct_chg"] >= 0.02 and row["close_near_high"] <= 0.30):
        return True, "A", _volume_extra_base("B5", "A", "B5_A_two_day_huge_volume_up_confirms_strict", extra)

    return False, "", {}


def detect_buy_point_same_day(df: pd.DataFrame, idx: int, ts_code: str):
    row = df.iloc[idx]
    need_ma_cols = ["ma5", "ma10", "ma20", "ma30", "ma60"]
    if any(pd.isna(row[c]) for c in need_ma_cols):
        return "", {}
    funcs = [
        ("B5_持续巨量上涨", b5_huge_up_grade(df, idx)),
        ("B3_持续放量上涨", b3_continuous_up_grade(df, idx)),
        ("B2_放量上涨", b2_strong_up_grade(df, idx)),
        ("B4_缓慢放量上涨", b4_slow_up_grade(df, idx)),
    ]

    candidates = []
    for name, (ok, grade, extra) in funcs:
        if ok:
            candidates.append((GRADE_RANK.get(grade, 0), TYPE_PRIORITY.get(name, 0), name, grade, extra))

    if not candidates:
        return "", {}

    candidates.sort(reverse=True)
    _, _, name, grade, extra = candidates[0]
    extra = dict(extra or {})
    extra["buy_point_type_base"] = name
    extra["buy_point_candidates"] = "; ".join([f"{x[2]}_{x[3]}" for x in candidates])
    return name, extra


# =========================
# 备注与输出行
# =========================
def detect_buy_point_same_day_book(df: pd.DataFrame, idx: int, ts_code: str):
    row = df.iloc[idx]
    need_ma_cols = ["ma5", "ma10", "ma20", "ma30", "ma60"]
    if any(pd.isna(row[c]) for c in need_ma_cols):
        return "", {}

    funcs = [
        ("B5_持续巨量上涨", b5_book_huge_up_grade(df, idx)),
        ("B2_放量上涨", b2_book_strong_up_grade(df, idx)),
        ("B3_持续放量上涨", b3_book_continuous_up_grade(df, idx)),
        ("B4_缓慢放量上涨", b4_book_slow_up_grade(df, idx)),
    ]

    candidates = []
    for name, (ok, grade, extra) in funcs:
        if ok:
            candidates.append((GRADE_RANK.get(grade, 0), BUY_POINT_TYPE_PRIORITY.get(name, 0), name, grade, extra))

    if not candidates:
        return "", {}

    filter_detail = buy_point_filter_detail(row)
    if filter_detail.get("buy_point_filter_reject_reason"):
        filter_detail["buy_point_candidates_before_filter"] = "; ".join([f"{x[2]}_{x[3]}" for x in candidates])
        return "", filter_detail

    candidates.sort(reverse=True)
    _, _, name, grade, extra = candidates[0]
    extra = dict(extra or {})
    extra["buy_point_ruleset"] = BUY_POINT_RULESET
    extra["buy_point_type_base"] = name
    extra["buy_point_candidates"] = "; ".join([f"{x[2]}_{x[3]}" for x in candidates])
    return name, extra


def recent_fb_retrigger_no_break_ma20_detail(df: pd.DataFrame, idx: int, lookback_days: int = 60) -> dict:
    detail = {
        "fb_retrigger_60d_no_break_ma20": False,
        "fb_retrigger_60d_tag": "",
        "fb_retrigger_60d_prior_signal_date": "",
        "fb_retrigger_60d_days_since_prior": np.nan,
        "fb_retrigger_60d_remark": "FB_RETRIGGER_60D_NOT_TRIGGERED",
    }

    if idx <= 0 or pd.isna(df.iloc[idx].get("ma20")):
        return detail

    current_detail = five_buddha_scored_detail(df, idx)
    if not current_detail.get("fb_candidate", False):
        detail["fb_retrigger_60d_remark"] = "FB_RETRIGGER_60D_CURRENT_NOT_FB"
        return detail

    start = max(0, idx - lookback_days)
    for j in range(idx - 1, start - 1, -1):
        prior_detail = five_buddha_scored_detail(df, j)
        if not prior_detail.get("fb_candidate", False):
            continue

        segment = df.iloc[j: idx + 1]
        if segment.empty or segment[["close", "ma20"]].isna().any().any():
            continue

        never_broke_ma20 = bool((segment["close"] >= segment["ma20"]).all())
        if not never_broke_ma20:
            continue

        detail["fb_retrigger_60d_no_break_ma20"] = True
        detail["fb_retrigger_60d_tag"] = "60日内二次五佛手且期间未破MA20"
        detail["fb_retrigger_60d_prior_signal_date"] = df.iloc[j]["date"].strftime("%Y-%m-%d")
        detail["fb_retrigger_60d_days_since_prior"] = int(idx - j)
        detail["fb_retrigger_60d_remark"] = "FB_RETRIGGER_60D_PRIOR_FB_AND_NO_CLOSE_BELOW_MA20"
        return detail

    detail["fb_retrigger_60d_remark"] = "FB_RETRIGGER_60D_NO_PRIOR_FB_WITH_MA20_SUPPORT"
    return detail


def make_common_row(ticker: str, ts_code: str, name: str, target_date: str, row: pd.Series, sw_l1_detail: dict = None) -> dict:
    ma_distance_grade, ma_distance_remark = ma_distance_grade_from_spread(row.get("five_line_spread"))
    new_high_remark = targetday_new_high_30d_remark(row)
    close_ma5_distance, close_ma5_grade, close_ma5_remark = close_ma5_distance_grade(row.get("close"), row.get("ma5"))
    ma_detail = ma_structure_detail(row)
    sw_l1_detail = dict(sw_l1_detail or {})

    return {
        "ticker": ticker,
        "ts_code": ts_code,
        "name": name,
        "target_date": target_date,
        "price_adjust_mode": PRICE_ADJ_MODE if PRICE_ADJ_MODE else "none",
        "price_adjust_anchor_date": PRICE_ADJ_ANCHOR_DATE,
        "fetch_end_date": FETCH_END_DATE,
        "signal_date": row["date"].strftime("%Y-%m-%d"),
        "price_adjust_mode": PRICE_ADJ_MODE,
        "close": round(float(row["close"]), 2),
        "open": round(float(row["open"]), 2),
        "high": round(float(row["high"]), 2),
        "low": round(float(row["low"]), 2),
        "pct_chg_pct": round(float(row["pct_chg"]) * 100, 2) if pd.notna(row["pct_chg"]) else np.nan,
        "volume": round(float(row["volume"]), 0),
        "turnover_rate": round(float(row["turnover_rate"]), 2) if pd.notna(row.get("turnover_rate")) else np.nan,
        "vol_vs_ma5": round(safe_ratio(row["volume"], row["vol_ma5"]), 2),
        "vol_vs_ma10": round(safe_ratio(row["volume"], row["vol_ma10"]), 2),
        "vol_vs_ma20": round(safe_ratio(row["volume"], row["vol_ma20"]), 2),
        "ma5": round(float(row["ma5"]), 2) if pd.notna(row["ma5"]) else np.nan,
        "targetday_close_over_ma5_pct": round(float(close_ma5_distance) * 100, 2) if pd.notna(close_ma5_distance) else np.nan,
        "targetday_close_ma5_distance_grade": close_ma5_grade,
        "targetday_close_ma5_distance_remark": close_ma5_remark,
        "ma10": round(float(row["ma10"]), 2) if pd.notna(row["ma10"]) else np.nan,
        "ma20": round(float(row["ma20"]), 2) if pd.notna(row["ma20"]) else np.nan,
        "ma30": round(float(row["ma30"]), 2) if pd.notna(row["ma30"]) else np.nan,
        "ma60": round(float(row["ma60"]), 2) if pd.notna(row["ma60"]) else np.nan,
        "ma250": round(float(row["ma250"]), 2) if pd.notna(row.get("ma250")) else np.nan,
        "body_ratio": round(float(row["body_ratio"]), 4) if pd.notna(row["body_ratio"]) else np.nan,
        "close_near_high": round(float(row["close_near_high"]), 4) if pd.notna(row["close_near_high"]) else np.nan,
        "targetday_is_30d_close_high": bool(row["is_30d_close_high"]) if pd.notna(row.get("is_30d_close_high")) else np.nan,
        "targetday_is_30d_intraday_high": bool(row["is_30d_intraday_high"]) if pd.notna(row.get("is_30d_intraday_high")) else np.nan,
        "targetday_close_30d_high": round(float(row["close_30d_high"]), 2) if pd.notna(row.get("close_30d_high")) else np.nan,
        "targetday_high_30d_high": round(float(row["high_30d_high"]), 2) if pd.notna(row.get("high_30d_high")) else np.nan,
        "targetday_30d_new_high_remark": new_high_remark,
        "targetday_ma_max_gap_pct": round(float(row["five_line_spread"]) * 100, 2) if pd.notna(row.get("five_line_spread")) else np.nan,
        "targetday_ma_distance_grade": ma_distance_grade,
        "targetday_ma_distance_remark": ma_distance_remark,
        **sw_l1_detail,
        **ma_detail,
    }


def evaluate_one_stock_multi_dates(pro, ts_code: str, ticker: str, name: str, target_dates, stock_sw_history_map):
    obs_list = []
    debug_list = []
    failed_list = []

    try:
        raw = fetch_tushare_bar_with_retry(pro, ts_code, START_DATE, FETCH_END_DATE, max_retry=3)
    except Exception as e:
        failed_list.append({
            "ticker": ticker,
            "ts_code": ts_code,
            "name": name,
            "target_date": "",
            "error": f"qfq_fetch_exception: {type(e).__name__}: {e}",
        })
        return obs_list, debug_list, failed_list

    try:
        df_full = standardize_tushare_bar(raw)
        df_full = build_indicators(df_full)
    except Exception as e:
        failed_list.append({
            "ticker": ticker,
            "ts_code": ts_code,
            "name": name,
            "target_date": "",
            "error": f"standardize_or_indicator_exception: {type(e).__name__}: {e}",
        })
        return obs_list, debug_list, failed_list

    if len(df_full) < MIN_HISTORY_BARS:
        failed_list.append({
            "ticker": ticker,
            "ts_code": ts_code,
            "name": name,
            "target_date": "",
            "error": f"not_enough_history_bars: {len(df_full)}",
        })
        return obs_list, debug_list, failed_list

    for target_date in target_dates:
        df = slice_df_to_target_date(df_full, target_date)
        if df.empty:
            debug_list.append({
                "ticker": ticker, "ts_code": ts_code, "name": name,
                "target_date": target_date, "reject_reason": "no_data_before_target_date",
            })
            continue

        row = df.iloc[-1]
        idx = len(df) - 1
        actual_date = row["date"].strftime("%Y%m%d")
        if actual_date != str(target_date):
            debug_list.append({
                "ticker": ticker, "ts_code": ts_code, "name": name,
                "target_date": target_date, "actual_last_trade_date": actual_date,
                "reject_reason": "target_date_not_trading_day_or_no_data",
            })
            continue

        sw_l1_detail = build_sw_l1_snapshot_detail(pro, stock_sw_history_map.get(ts_code, []), target_date)
        fb_detail = five_buddha_scored_detail(df, idx)
        retrigger_detail = recent_fb_retrigger_no_break_ma20_detail(df, idx, lookback_days=60)

        if fb_detail.get("fb_candidate", False):
            obs_row = {
                **make_common_row(ticker, ts_code, name, target_date, row, sw_l1_detail=sw_l1_detail),
                **fb_detail,
                **retrigger_detail,
            }
            obs_row["remark_summary"] = join_remarks(
                obs_row.get("fb_candidate_reason"),
                obs_row.get("sw_l1_above_ma20_remark"),
                obs_row.get("fb_retrigger_60d_tag"),
                obs_row.get("targetday_30d_new_high_remark"),
                obs_row.get("targetday_ma_distance_remark"),
                obs_row.get("targetday_close_ma5_distance_remark"),
            )
            obs_list.append(obs_row)
        else:
            debug_list.append({
                **make_common_row(ticker, ts_code, name, target_date, row, sw_l1_detail=sw_l1_detail),
                **fb_detail,
                **retrigger_detail,
                "reject_reason": fb_detail.get("fb_hard_fail_reason", "fb_not_candidate"),
            })

    return obs_list, debug_list, failed_list


def build_lite_output(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    common_cols = [
        "ticker", "name", "target_date", "signal_date",
        "close", "pct_chg_pct", "volume", "turnover_rate",
        "ma5", "ma10", "ma20", "ma30", "ma60", "ma250",
        "sw_l1_name", "sw_l1_index_code", "sw_l1_close", "sw_l1_ma20", "sw_l1_above_ma20", "sw_l1_above_ma20_remark",
        "fb_candidate_reason",
        "ma_structure_ok", "ma_structure_reason",
        "fb_retrigger_60d_no_break_ma20", "fb_retrigger_60d_tag", "fb_retrigger_60d_prior_signal_date",
        "fb_retrigger_60d_days_since_prior", "fb_retrigger_60d_remark",
        "targetday_ma_distance_grade", "targetday_close_ma5_distance_grade",
        "targetday_30d_new_high_remark", "remark_summary",
    ]
    wanted = common_cols
    wanted = [c for c in wanted if c in df.columns]
    return df[wanted].copy()


# =========================
# 主函数
# =========================
def main():
    if not TUSHARE_TOKEN:
        raise RuntimeError("请先设置环境变量 TUSHARE_TOKEN，或在脚本中填写 TUSHARE_TOKEN。")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts.set_token(TUSHARE_TOKEN)
    pro = ts.pro_api()
    initialize_runtime_dates(pro)

    universe, filtered_out = load_universe(pro)
    filtered_out.to_csv(FILTERED_FILE, index=False, encoding="utf-8-sig")
    stock_sw_history_map = build_stock_sw_l1_history_map(pro, universe)

    print("TARGET_DATES:", TARGET_DATES)
    print("SCAN_END_DATE:", SCAN_END_DATE)
    print("PRICE_ADJ_ANCHOR_DATE:", PRICE_ADJ_ANCHOR_DATE)
    print("FETCH_END_DATE:", FETCH_END_DATE)
    print("PRICE_ADJ_MODE:", PRICE_ADJ_MODE)
    print("universe size:", len(universe))
    print("filtered_out size:", len(filtered_out))
    print("sw_l1_history_mapped stocks:", len(stock_sw_history_map))

    all_obs = []
    all_debug = []
    all_failed = []

    tasks = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for _, r in universe.iterrows():
            tasks.append(executor.submit(
                evaluate_one_stock_multi_dates,
                pro,
                r["ts_code"],
                r["ticker"],
                r["name"],
                TARGET_DATES,
                stock_sw_history_map,
            ))
            time.sleep(SLEEP_SEC)

        for i, fut in enumerate(as_completed(tasks), 1):
            try:
                obs, debug, failed = fut.result()
                all_obs.extend(obs)
                all_debug.extend(debug)
                all_failed.extend(failed)
            except Exception as e:
                all_failed.append({"error": f"future_exception: {type(e).__name__}: {e}"})
            if i % 50 == 0:
                print(f"processed {i}/{len(tasks)} | pool={len(all_obs)} | debug={len(all_debug)} | failed={len(all_failed)}")

    obs_df = pd.DataFrame(all_obs)
    debug_df = pd.DataFrame(all_debug)
    failed_df = pd.DataFrame(all_failed)

    if not obs_df.empty:
        obs_df = obs_df.sort_values(["target_date", "ticker"], ascending=[True, True]).reset_index(drop=True)

    obs_df.to_csv(OBSERVATION_FILE, index=False, encoding="utf-8-sig")
    debug_df.to_csv(DEBUG_FILE, index=False, encoding="utf-8-sig")
    failed_df.to_csv(FAILED_FILE, index=False, encoding="utf-8-sig")

    obs_lite_df = build_lite_output(obs_df)
    obs_lite_df.to_csv(OBSERVATION_LITE_FILE, index=False, encoding="utf-8-sig")

    print("done")
    print("pool file:", OBSERVATION_FILE, "rows:", len(obs_df))
    print("pool lite file:", OBSERVATION_LITE_FILE, "rows:", len(obs_lite_df))
    print("debug file:", DEBUG_FILE, "rows:", len(debug_df))
    print("failed file:", FAILED_FILE, "rows:", len(failed_df))

    if not obs_df.empty:
        show_cols = [
            "ticker", "name", "target_date", "signal_date",
            "sw_l1_name", "sw_l1_above_ma20",
            "fb_candidate_reason", "price_adjust_mode", "close", "pct_chg_pct", "turnover_rate",
            "targetday_close_ma5_distance_grade", "targetday_ma_distance_grade", "remark_summary",
        ]
        show_cols = [c for c in show_cols if c in obs_df.columns]
        print(obs_df[show_cols].head(30).to_string(index=False))

    return {
        "observation_file": OBSERVATION_FILE,
        "observation_lite_file": OBSERVATION_LITE_FILE,
        "debug_file": DEBUG_FILE,
        "failed_file": FAILED_FILE,
        "filtered_file": FILTERED_FILE,
        "observation_rows": len(obs_df),
        "observation_lite_rows": len(obs_lite_df),
        "debug_rows": len(debug_df),
        "failed_rows": len(failed_df),
        "target_dates": TARGET_DATES,
        "universe_size": len(universe),
    }


if __name__ == "__main__":
    main()
