# market-monitor：美股 ETF 状态监控（低频、规则驱动）

**分工**：Python（GitHub Actions 免费运行）负责数字；Claude 定时任务负责解读和提醒。
**原则**：每月按规则决策，每日只做监控；绝大多数日子应当是"⚪ 无事"。

## 每天发生什么
| 新加坡时间 | 谁 | 做什么 |
|---|---|---|
| 07:37（周二–周六） | GitHub Actions | 拉取 FRED 宏观/信用数据和 ETF 价格 → 计算指标 → 对照 `config/ips.yaml` 规则 → 写入 `reports/` 和 `data/` 并提交 |
| 约 08:1x | Claude 定时任务 | 读取 `reports/latest.json`，按报警等级决定检索多少，生成简报（见 `CLAUDE_TASK.md`） |

## 报警等级
- 🔴 **红色（规则触发，需要行动）**：抄底阶梯越过新档位；"两把钥匙"降风险规则触发或解除
- 🟡 **黄色（观察，不行动）**：VIX ≥ 30、VIX 期限结构倒挂、高收益债利差 20 日走阔、NFCI > 0、跌破 200 日线、单日 ±4%、只有一把钥匙转动
- ⚪ **白色**：无事

## 文件
- `config/ips.yaml`：你的投资规则和阈值（**阈值都是待回测的初始参数**；规则每年最多改一次）
- `monitor/`：采集（`fetch.py`）、指标（`indicators.py`）、规则（`rules.py`）、报告（`report.py`）、入口（`run.py`）
- `data/snapshots.csv`：只追加的点时快照，以后用于复盘和回测，不会产生前视偏差
- `data/state.json`：规则状态（同一档位只报警一次）
- `reports/latest.json`、`reports/daily/*.md`：机器摘要和每日日报
- `tests/`：离线测试（合成数据），每次运行前自动执行

## 上线步骤
1. 在 GitHub 新建仓库（公开仓库的 Actions 分钟数不限；私有仓库在免费计划下每月 2,000 分钟，本任务每月约用 45 分钟）。
2. 上传本文件夹全部内容（包括 `.github/workflows/daily.yml`）。
3. 推送代码后工作流会自动运行一次（也可在 **Actions → daily-monitor → Run workflow** 手动运行），确认 `reports/` 下生成了文件。
4. 把仓库地址告诉 Claude，用于设置每日定时任务。

## 已知限制
- yfinance 是非官方接口，在 GitHub 的共享 IP 上可能被限流；失败时会回退到 Stooq（未做股息复权），并写入 `data_issues`。
- FRED 的部分序列（如 ICE 高收益债利差）的历史长度可能受授权限制；12 个月均值只需要最近约 252 个交易日。
- GitHub 的定时任务可能延迟几分钟到几十分钟；公开仓库如果 60 天没有任何活动，定时任务会被自动停用（每日自动提交通常就算活动）。
- 美股休市日价格不会更新，系统会标注"数据未更新"，并且不会重复报警。
