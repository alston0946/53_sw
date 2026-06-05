# 五佛手策略说明

对应脚本：

- `五三形态_五佛手_20260520_ma250_qfq.py`

当前版本只做你要的五佛手 + 书本版 `B2 / B3 / B4 / B5`，并且已经按你的要求调整为更严格的确认逻辑：

- 删除 `fb_score`
- 删除 `B1`
- `30日新高` 改为硬条件
- 最近两日换手率同时过低直接淘汰
- `targetday` 确认改为更严格的单一通过制，不再分 `S/A/B/C` 多档

## 一、整体流程

1. 读取股票池，过滤 `ST` 和 `below_8b`
2. 用 `Tushare pro.daily + pro.adj_factor + pro.daily_basic` 拉取日线、复权因子、换手率
3. 做 `qfq` 前复权
4. 计算均线、量均线、30日新高、换手率、K线位置指标
5. 判断目标日是否满足五佛手硬条件
6. 如果目标日当天又满足书本版 `B2 / B3 / B4 / B5` 的严格确认，则进入 `buy_confirm`

输出含义：

- `observation`：目标日自身是五佛手
- `buy_confirm`：目标日命中严格买点确认，并且向前回看 `5` 个交易日内出现过五佛手

注意：

- `buy_confirm` 不是要求 `targetday` 自身仍然是五佛手
- 它的逻辑是“前面最近出现过五佛手 + 当天出现严格确认买点”

## 二、核心参数

### 1. 五佛手参数

- `FB_LOOKBACK = 15`
- `FB_RECENT_CONVERGE_MAX = 0.12`
- `FB_CURRENT_SPREAD_MAX = 0.20`
- `FB_MAX_CLOSE_OVER_MA20 = 0.15`
- `FB_MA30_3D_FLOOR = -0.010`
- `FB_MA60_5D_FLOOR = -0.015`
- `FB_NEW_HIGH_LOOKBACK = 30`
- `FB_TURNOVER_RATE_MIN = 1.5`
- `FB_CONFIRM_LOOKBACK_DAYS = 5`

### 2. 目标确认日过滤参数

- `BUY_POINT_RULESET = "book"`
- `BUY_POINT_FILTER_REQUIRE_CLOSE_ABOVE_ALL_MA = True`
- `BUY_POINT_FILTER_REQUIRE_TARGET_ABOVE_MA5 = True`
- `BUY_POINT_FILTER_REQUIRE_TARGET_BULLISH_BAR = True`
- `BUY_POINT_FILTER_REQUIRE_TARGET_POSITIVE_PCT = True`
- `BUY_POINT_FILTER_TARGET_CLOSE_NEAR_HIGH_MAX = 0.45`

### 3. B2 / B3 / B4 / B5 参数

- `B2_STRONG_UP_PCT = 0.040`
- `B2_CLOSE_NEAR_HIGH_MAX = 0.25`
- `B3_BOOK_VOL_EQUAL_TOL = 0.95`
- `B4_DAILY_DROP_FLOOR = -0.02`
- `B4_5D_TOTAL_UP_MIN = 0.03`
- `B4_5D_TOTAL_UP_MAX = 0.08`
- `B4_BOOK_VOL_TAIL_HEAD_RATIO = 1.05`
- `B5_VOL_VS_MA20_LOOSE = 2.00`
- `B5_2D_TOTAL_UP_STRICT = 0.06`

### 4. 买点优先级

同一天如果同时命中多个买点，按下面优先级选最终类型：

- `B5` 优先级 `40`
- `B2` 优先级 `30`
- `B3` 优先级 `20`
- `B4` 优先级 `10`

当前 `volume_confirm_grade` 只有一种有效值：

- `A`：通过严格确认

## 三、指标公式

### 1. 价格均线

- `MA_n = close.rolling(n).mean()`

当前计算：

- `MA5 / MA10 / MA20 / MA30 / MA60 / MA120 / MA240 / MA250`

### 2. 成交量均线

- `VOL_MA_n = volume.rolling(n).mean()`

当前计算：

- `vol_ma5 / vol_ma10 / vol_ma20`

### 3. K线位置指标

- `bar_range_abs = max(high - low, 1e-8)`
- `body = abs(close - open)`
- `body_ratio = body / bar_range_abs`
- `close_near_high = (high - close) / bar_range_abs`
- `range_pct = (high - low) / close`

说明：

- `close_near_high` 越小，说明收盘越靠近日内高点
- 例如 `0.25` 表示收盘离最高点不超过当日振幅的 `25%`

### 4. 五线发散度

只使用：

- `MA5`
- `MA10`
- `MA20`
- `MA30`
- `MA60`

公式：

- `five_line_spread = (max(MA5, MA10, MA20, MA30, MA60) - min(MA5, MA10, MA20, MA30, MA60)) / close`

最近 `15` 个交易日内最紧的一次：

- `recent_min_spread = min(five_line_spread in last 15 bars)`

### 5. 30日新高指标

- `close_30d_high = close.rolling(30, min_periods=30).max()`
- `high_30d_high = high.rolling(30, min_periods=30).max()`
- `is_30d_close_high = close >= close_30d_high`
- `is_30d_intraday_high = high >= high_30d_high`

### 6. 换手率指标

来自 `Tushare daily_basic`：

- `turnover_rate`

硬过滤条件：

- `today_turnover_rate < 1.5 and yesterday_turnover_rate < 1.5 -> reject`

## 四、五佛手硬条件

只要有一条不满足，就不是当前版本的五佛手。

### 1. 历史长度

- 至少 `260` 根K线

### 2. 收盘站位

- `close >= MA5`
- `close >= MA10`
- `close >= MA20`
- `close >= MA30`
- `close >= MA60`
- `close >= MA250`

### 3. 均线结构

- `MA5 > MA10`
- `MA20 < min(MA5, MA10)`
- `MA30 < min(MA5, MA10)`
- `MA60 < min(MA5, MA10)`
- `MA250 < min(MA5, MA10)`

### 4. 粘合与发散

- 最近 `15` 个交易日内至少一次满足 `recent_min_spread <= 0.12`
- 目标日当天满足 `five_line_spread <= 0.20`

### 5. 收盘不能离 MA20 太远

- `close / MA20 - 1 <= 0.15`

也就是收盘最多高出 `MA20` 的 `15%`。

### 6. 中期均线不能明显走坏

- `ma30 / ma30.shift(3) - 1 >= -0.010`
- `ma60 / ma60.shift(5) - 1 >= -0.015`

### 7. 30日新高必须通过

下面至少满足一个：

- `is_30d_close_high == True`
- `is_30d_intraday_high == True`

### 8. 最近两日换手率不能同时过低

- `today_turnover_rate < 1.5 and yesterday_turnover_rate < 1.5` 时直接淘汰

## 五、targetday 严格确认过滤

这一层是为了避免“前面曾经是好形态，但确认日本身已经明显转弱”的漏洞。

目标确认日必须同时满足：

- `close >= MA5`
- `close >= MA5 / MA10 / MA20 / MA30 / MA60 / MA250`
- `close >= open`
- `pct_chg > 0`
- `close_near_high <= 0.45`

如果不满足，会在调试文件里出现类似拒绝原因：

- `BUY_FILTER_CLOSE_BELOW_MA5`
- `BUY_FILTER_CLOSE_BELOW_MA10...`
- `BUY_FILTER_NOT_BULLISH_BAR`
- `BUY_FILTER_TARGET_PCT_NOT_POSITIVE`
- `BUY_FILTER_TARGET_CLOSE_NOT_NEAR_HIGH`

## 六、书本版买点确认逻辑

当前只保留严格通过制，不再分 `S/A/B/C` 多档。

### 1. B2 放量上涨

使用最近 `2` 根K线，必须同时满足：

- 前一天上涨：`prev.pct_chg > 0`
- 当天上涨：`row.pct_chg > 0`
- 前一天阳线：`prev.close >= prev.open`
- 当天阳线：`row.close >= row.open`
- 两日整体上涨：`row.close > prev.close`
- 当天涨幅至少 `4%`：`row.pct_chg >= 0.04`
- 两日总涨幅至少 `4%`
- 当天成交量大于前一天
- 当天成交量大于 `vol_ma5 / vol_ma10 / vol_ma20`
- 当天量比前一天至少放大 `1.2` 倍
- 当天收盘靠近日高：`close_near_high <= 0.25`

通过后记为：

- `buy_point_type = B2_放量上涨`
- `volume_confirm_grade = A`

### 2. B3 持续放量上涨

使用最近 `3` 根K线，必须同时满足：

- 三天全为上涨：`up_days == 3`
- 三天全为阳线：`bullish_days == 3`
- 三天整体上涨
- 三天总涨幅至少 `4%`
- 量能满足温和递增：`vol2 >= vol1 * 0.95` 且 `vol3 >= vol2`
- 第三天成交量是三天中最高
- 第三天成交量大于 `vol_ma10` 和 `vol_ma20`
- 第三天收盘靠近日高：`close_near_high <= 0.35`

通过后记为：

- `buy_point_type = B3_持续放量上涨`
- `volume_confirm_grade = A`

### 3. B4 缓慢放量上涨

这里只保留更严格的 `5日窗口`，不再使用 `4日 / 3日` 放宽版本。

最近 `5` 根K线必须同时满足：

- `total_up = close_last / close_first - 1`
- `0.03 <= total_up <= 0.08`
- `up_days >= 4`
- `bullish_days >= 4`
- 窗口内单日最大跌幅不能低于 `-2%`
- 后半段平均量能至少高于前半段 `1.05` 倍
- 最后一天量能不能明显走弱：`last_volume >= median(volume_window) * 0.95`
- 最后一天必须是阳线且涨幅为正
- 最后一天收盘必须是这 `5` 天里的收盘新高
- 最后一天收盘靠近日高：`close_near_high <= 0.40`

这条“最后一天收盘必须是5日收盘新高”就是这次专门加的，主要用来堵住你说的那种“前面涨过了，但最后一天明显回吐很多还通过”的漏洞。

通过后记为：

- `buy_point_type = B4_缓慢放量上涨`
- `volume_confirm_grade = A`

### 4. B5 持续巨量上涨

使用最近 `2` 根K线，必须同时满足：

- 两天都上涨
- 两天都收阳
- 两天成交量都至少达到各自 `vol_ma20` 的 `2.0` 倍
- 第二天量能不能比第一天缩太多：`target_volume >= prev_volume * 0.90`
- 两天总涨幅至少 `6%`
- 第二天涨幅至少 `2%`
- 第二天收盘靠近日高：`close_near_high <= 0.30`

通过后记为：

- `buy_point_type = B5_持续巨量上涨`
- `volume_confirm_grade = A`

## 七、60日内二次五佛手标志

如果当前选中的目标日满足：

- 当前目标日本身是五佛手
- 在之前 `60` 个交易日内出现过更早一次五佛手
- 从前一次五佛手到当前这一次之间，`close` 从未跌破 `MA20`

则输出：

- `fb_retrigger_60d_no_break_ma20 = True`
- `fb_retrigger_60d_tag = 60日内二次五佛手且期间未破MA20`

这个标记的含义是：

- 不是第一次五佛手
- 期间趋势没有被 `MA20` 破坏
- 属于“二次出现、但中途结构没坏”的更强延续型案例

## 八、输出文件说明

### 1. 主要文件

- `five_buddha_observation_pool_...csv`
  说明：目标日本身满足五佛手

- `five_buddha_buy_confirm_pool_...csv`
  说明：目标日出现严格买点确认，且近 `5` 日内出现过五佛手

- `five_buddha_debug_rejected_...csv`
  说明：用于看没通过的原因

- `..._lite.csv`
  说明：精简版输出，只保留更重要字段

### 2. 关键字段解释

- `target_date`
  你设置的扫描目标日

- `signal_date`
  当前这一行对应的实际信号日期，通常和 `target_date` 一致

- `fb_signal_date`
  最近一次五佛手出现的日期

- `days_after_fb`
  从 `fb_signal_date` 到 `target_date` 相隔多少个交易日

- `buy_point_type`
  最终命中的书本买点类型，如 `B2_放量上涨`

- `buy_point_type_base`
  买点原始类型名，便于程序排序

- `volume_confirm_grade`
  当前版本只有 `A` 一种有效值，表示严格通过

- `volume_confirm_remark`
  对应买点通过的英文备注码，方便你回头筛选

- `targetday_ma_distance_grade`
  目标日五线发散度等级
  `G1_3%以内`：五线非常粘
  `G2_3%-6%`：正常
  `G3_6%以上`：发散偏大

- `targetday_close_ma5_distance_grade`
  收盘价距离 `MA5` 的偏离等级
  `G1_小于等于5%`：离 `MA5` 不远
  `G2_大于5%`：离 `MA5` 偏远

- `targetday_30d_new_high_remark`
  说明目标日是通过“收盘30日新高”还是“盘中30日新高”过关

- `remark_summary`
  把五佛手原因、买点原因、均线状态、30日新高状态、二次五佛手标记等拼成一列，方便直接浏览

- `reject_reason`
  在 `debug` 文件里用于说明淘汰原因

## 九、当前版本最重要的理解

这版策略的核心不是“样本尽量多”，而是：

- 五佛手本体先要干净
- `targetday` 确认日自己也不能太弱
- 尤其 `B4` 不能再出现最后一天明显走坏还被放进确认池的情况

所以现在的方向是：

- 宁可少一点样本
- 也尽量保证确认日形态更完整、更接近你想要的强势确认
