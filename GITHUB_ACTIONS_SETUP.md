# GitHub 自动运行与 QQ 邮箱配置

这个项目已经补好了 GitHub Actions 自动运行入口，默认会在每个工作日北京时间 18:35 左右运行一次。

## 需要上传到仓库的文件

- `五三形态_五佛手_简洁版_申万行业.py`
- `run_and_email.py`
- `requirements.txt`
- `.github/workflows/run_five_buddha.yml`
- `.gitignore`

## GitHub Secrets

在仓库 `Settings` -> `Secrets and variables` -> `Actions` 里添加这些 `Secrets`：

- `TUSHARE_TOKEN`：你的 Tushare token
- `SMTP_HOST`：`smtp.qq.com`
- `SMTP_PORT`：`465`
- `SMTP_USER`：你的 QQ 邮箱，例如 `123456789@qq.com`
- `SMTP_PASSWORD`：QQ 邮箱 SMTP 授权码，不是登录密码
- `EMAIL_TO`：收件人邮箱，多个可用英文逗号分隔

## GitHub Variables

可选添加这些 `Variables`：

- `EMAIL_SUBJECT_PREFIX`：邮件标题前缀，例如 `五佛手申万行业`
- `TARGET_DATES`：指定目标日期，多个日期用逗号分隔，例如 `20260605,20260606`
- `TARGET_DATE_RANGES`：指定日期区间，格式如 `20260601:20260605,20260610:20260612`
- `PRICE_ADJ_ANCHOR_DATE`：前复权锚定日，不填时默认取上海时区当天
- `EMAIL_ATTACH_DEBUG`：填 `true` 时，邮件额外附带 debug/failed/filtered 文件

## 关于本地 CSV

脚本现在支持两种股票池来源：

1. 仓库内存在 `data/a_share_codes_for_akshare.csv` 时，优先使用你自己的 CSV。
2. 如果仓库里没有这个文件，会自动走 Tushare 的 `stock_basic + daily_basic` 构建股票池，并自动过滤：
   - 名称中含 `ST`
   - 总市值小于 80 亿

## 首次推送后怎么验证

1. 打开仓库 `Actions`
2. 运行 `Run Five Buddha Strategy`
3. 看日志里是否出现 `Email sent successfully.`
4. 去邮箱确认是否收到附件

## QQ 邮箱注意事项

- QQ 邮箱必须先开启 SMTP 服务
- `SMTP_PASSWORD` 要填 QQ 邮箱生成的授权码
- 如果收不到邮件，先看 GitHub Actions 日志里的 SMTP 报错
