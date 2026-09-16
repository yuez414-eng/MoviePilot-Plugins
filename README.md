# MoviePilot-Plugins

自建 MoviePilot 插件市场（Third-party market）。

目前包含 1 个插件：**硬链接与识别校验（HardlinkVerify）**

---

## 这个插件解决什么问题

MoviePilot 的 TMDB 匹配有一类已知误配：当种子文件名里带着「季年份」时，
MP 会把那个年份当作剧集首播年份去搜；而 TMDB 上存在一些「首播年份刚好等于该年份、
且带同名译名」的低热度脏词条，`__search_tv_by_season()` 按 `first_air_date` 倒序
取第一个命中，于是脏词条先于正确条目被返回。

**真实案例**：`Supernatural.2017.S13E09`

- 文件名解析出的年份 = 2017
- TMDB 脏词条 `261164`（소능력자 / KR / 2017-06-14 / vote_count=1）的英文译名恰好是 `Supernatural`
- 正确条目 `1622`（邪恶力量 / 2005）排在它后面
- 结果：23 个文件被改名成 `소능력자` 并归进「日韩剧」

插件把这类问题在**进库之后、你发现之前**捞出来。

## 三层校验

| 层级 | 手段 | 代价 | 能抓到什么 |
|---|---|---|---|
| L1 | 硬链接 inode 比对（`st_dev`+`st_ino`） | 纯本地 | 媒体库文件变成独立副本 / 目标文件丢失 |
| L2 | 用当前识别词重新解析源文件名，比对年份、季号 | 纯本地，零外部请求 | 「文件名年份 ≠ 条目年份」这类高风险命名模式 |
| L3 | 走 MP 真实识别链 `MediaChain().recognize_media()` 调 TMDB，比对 tmdbid / vote_count / 总季数 / 原产国 | 消耗 TMDB 请求（有缓存） | 真正的识别错配 —— 记录与识别器结论冲突、脏词条、季号越界、分类错位 |

### 关于误报抑制（这是这个插件最花心思的地方）

真实环境里同一个媒体库路径会被**反复整理覆盖**：同一集的 1080p 与 2160p 版本会
hardlink 到**同一个 dest**，后一次会让前一次记录的 inode 关系失效。
这不是故障，是预期行为。

所以插件会先回看额外 14 天建立「同路径 / 同剧同季 最新记录」索引，
**只判定每个 dest 的最新那条记录**，被覆盖的旧记录仅计数、不计为异常。

同时 L2 的结论默认只是「待定」，必须由 L3 确认后才会上报 ——
因为多季剧把当季年份写进文件名是普遍做法，本身并不是错。

而且「L3 确认」的门槛是**只有 actionable 级别（error/warn）的证据才算数**：
info 级别的证据（别名太少、分类目录与原产国不符等）不足以把一条待定项升级成异常。
年份冲突还有一条独立豁免 —— 若 TMDB 上该标题**同名条目唯一且 id 就是记录里的那个**，
则认定为「公映年/首播年口径差异」，降级为提示项 `year_delta_ok`。

实测效果（NAS 上 800 条整理记录）：

| 指标 | 优化前 | 优化后 |
|---|---|---|
| 上报项 | 1311 / 2000 | **32 / 800** |
| 有效信噪比 | ~0% | **≈4%** |

剩下 228 条被归类为「非异常提示」，折叠在详情页里，不触发通知。

若干轮抑制迭代之后（真实环境，2026-09-16 全量体检）：

| 指标 | 数 |
|---|---|
| 判定 | 2348 条 |
| **异常（notify）** | **0 条** |
| 非异常提示 | 982 条 |
| 其中被覆盖跳过（superseded） | 237 条 |
| 未挂载跳过 | 0 条 |
| 深度复核（TMDB） | 7 条 |
| 累计清理僵尸整理记录 | 54 条（分 5 轮收敛：23 → 16 → 8 → 6 → 1） |

分 5 轮才清完是因为「旧记录被新记录接管」的关系链是一层套一层的 ——
上层记录被清掉之后，下层那条才浮出来变成「不再被接管」。逐轮递减直到归零，
这是设计如此，不是异常。

---

## 装到别人机器上要填什么？—— 什么都不用填

这是最常被问到的点：**插件不需要你输入任何路径**。

数据源是 MoviePilot **自己的整理记录表 `transferhistory`** —— 也就是「整理记录」页面上
看到的那份清单。插件直接读这张表拿到每条记录的 `src`（下载目录源文件）、`dest`
（媒体库目标文件）、`files`（源文件列表）、`mode`（整理方式），然后自己去
`os.stat()` 比对 inode。所以：

| 你担心要手填的 | 实际 |
|---|---|
| 下载目录路径 | ❌ 不用。来自记录里的 `src` / `files` |
| 媒体库路径 | ❌ 不用。来自记录里的 `dest` |
| 分类目录结构 | ❌ 不用。按 MP 自己记的路径走 |
| 存储卷列表 | ❌ 不用。挂载状态自动从容器 `/proc/mounts` 读（v1.5.1 起） |
| API Key / Token | ❌ 不用。内部服务直接调 |

**唯一的硬性前置条件**（不满足则对应记录被跳过，不会误报也不会误删）：

1. **下载目录与媒体库目录都必须挂进 MoviePilot 容器。**
   `os.stat()` 只能看见容器里挂载了的东西。没挂进来的卷，插件会识别为
   「未挂载」并跳过（v1.5.1 起读取容器真实挂载表 `/proc/mounts` 判定，
   不再按 `/volumeN` 这类目录名硬编码 —— 群晖 `/volume1`、Docker 常见的
   `/data`、`/mnt/media` 等任意布局都能正确识别）。
2. **整理方式 `mode` 必须是 `link` / `hardlink`。** 如果 MP 是复制（copy）或移动
   （move）模式产的记录，本来就没有硬链接关系，L1 会被自然跳过 —— 只做 L2/L3
   识别校验。
3. **整理记录本身要有 `files`（源文件列表）。** 历史上极少数老记录没存，会退化成
   用单条 `src` 兜底；两者都空则跳过。

只要上面成立，装上、打开开关、设一个 cron，剩下的全自动。默认所有
「会动数据」的开关（自动补链、自动清理僵尸记录）都是**关闭**的。

## 仓库结构

```
MoviePilot-Plugins/
├── package.v2.json                 # 插件市场索引（MoviePilot v2 读这个）
├── package.json                    # 兼容索引（含 "v2": true 声明）
├── icons/
│   └── hardlinkverify.png          # 插件图标（512×512）
├── plugins.v2/                     # ★ v2 安装源（MP 实际从这里取包）
│   └── hardlinkverify/             #   目录名 = 插件ID小写（必须）
│       └── __init__.py
├── plugins/                        # v1 兜底目录（内容与 plugins.v2 一致）
│   └── hardlinkverify/
│       └── __init__.py
└── tools/
    ├── set_owner.py
    └── sync_plugins.py             # 保证上面两个目录一致
```

### ⚠️ 关键：v2 市场的源码目录是 `plugins.v2/`，不是 `plugins/`

这是本仓库踩过的坑，写清楚免得再犯。MoviePilot 取包时按
`app/helper/plugin.py::__async_get_file_list()` 这样拼 URL：

```python
file_api = f"https://api.github.com/repos/{user_repo}/contents/plugins"
if package_version:                      # package_version 来自 VERSION_FLAG，v2 时 = "v2"
    file_api += f".{package_version}"    # → "plugins.v2"
file_api += f"/{pid.lower()}"
```

所以：

| 索引 | `package_version` | MP 实际请求的目录 |
|---|---|---|
| `package.v2.json` 里有该插件 | `"v2"` | **`contents/plugins.v2/<pid>/`** |
| 只在 `package.json` 里声明 `"v2": true` | `""` | `contents/plugins/<pid>/` |

因为 `package.v2.json` 优先命中，本仓库走第一行 —— **源码必须放 `plugins.v2/`**。
放错目录的症状是安装时报：

```
连接仓库失败：404 - {"message":"Not Found",
  "documentation_url":"https://docs.github.com/rest/repos/contents#get-repository-content"}
```

（注意：市场**列表**能正常显示插件，只有点「安装」时才 404 —— 因为列表只读
`package.v2.json`，取包才走 `contents` API。）

下载落地时会做一次路径改写 `replace("plugins.v2", "plugins")`，
所以文件最终仍落在容器的 `/app/app/plugins/<pid>/` 下 —— 目录名只是**仓库侧的约定**。

官方与几个主流第三方 v2 市场（`jxxghp` / `thsrite` / `InfinityPacer` / `DDSRem-Dev` /
`hotlcc`）都是这个结构，可以自行核对。

### 索引与图标地址

1. 市场索引：`https://raw.githubusercontent.com/{user}/{repo}/main/package.{版本}.json`
   （v2 时是 `package.v2.json`，取不到会回落 `package.json`）
2. 文件清单：`https://api.github.com/repos/{user}/{repo}/contents/plugins.v2/{pid}`
3. 文件内容：清单里每项的 `download_url`（即 `raw.githubusercontent.com/...`）

改完源码记得跑一次同步，让两个目录一致：

```bash
python tools/sync_plugins.py           # 以 plugins.v2 为准覆盖 plugins
python tools/sync_plugins.py --check   # 只校验，不一致退出码 1
```


---

## 部署步骤

### 1. 填图标地址

图标用绝对 raw 地址（前端会把**裸文件名**解析到公共图标库，
自建仓库必须写全路径），所以先把占位符换成你的 GitHub 用户名：

```bash
python tools/set_owner.py <你的GitHub用户名>
```

### 2. 推到 GitHub

```bash
git init
git add .
git commit -m "feat: HardlinkVerify v1.2.0"
git branch -M main
git remote add origin https://github.com/<你的GitHub用户名>/MoviePilot-Plugins.git
git push -u origin main
```

公开仓库即可。私有仓库也能装，但要额外配 `REPO_GITHUB_TOKEN`（见下）。

> **token 权限要求**：`git push`、以及用 API 建仓（`POST /user/repos`）
> 都需要 classic PAT 勾选 **`repo`**（只要公开仓库的话最低 `public_repo`）。
> 一个 **scope 为空**的 token 只能读公开数据 —— `push` 会失败，
> 而且 API 建仓返回的是 **404 而不是 403**（GitHub 故意不泄露资源是否存在，
> 所以别把 404 当成"仓库重名/用户名写错"）。
>
> 查看自己 token 的 scope：
> ```bash
> curl -sI -H "Authorization: Bearer <token>" https://api.github.com/user | grep -i x-oauth-scopes
> # 空值 = 零权限
> ```

### 3. 让 MoviePilot 认识这个市场（不用改 compose）

`PLUGIN_MARKET` 在 MoviePilot v2 里是**可运行时编辑的设置项**，存在
`/config/app.env`，前端有专属对话框：

> **设置 → 插件市场 → 「插件市场设置」**

（前端组件 `PluginMarketSettingDialog`，保存调
`POST /api/v1/system/setting/PLUGIN_MARKET`，即时生效，**无需重启容器**。）

把仓库地址**追加到列表末尾**：

```
...,https://github.com/<你的GitHub用户名>/MoviePilot-Plugins
```

两点注意：

1. **保存是整体替换这个列表**，不是追加。所以先读出对话框里已回填的现有内容，
   在末尾用英文逗号接上自己的仓库，别把原有默认源清空。
2. 如果 compose 的 `environment:` 里也设了 `PLUGIN_MARKET`，**环境变量优先**，
   UI 保存会被拒绝并提示「已在环境变量中设置，请手动更新以保持一致性」。
   本机当前环境变量未设，走 `app.env`，所以 UI 可以直接改。

**最省心的顺序**：填上自己的仓库 → 保存 → 再点对话框里的
**「从 Wiki 同步」**。该动作会拉官方 Wiki
（`jxxghp/MoviePilot-Wiki/main/plugin.md`）的仓库清单，与本地列表
**合并去重（本地优先）**，把上游新增的市场源补回来，不会覆盖掉你新加的那条。

```bash
# 脚本化也行，一条 POST 就够（不用重启、不用登录 JWT）
# 认证头是 X-API-KEY，不是 Authorization: Bearer
curl -X POST 'http://<MoviePilot地址>:3002/api/v1/system/setting/PLUGIN_MARKET' \
     -H "X-API-KEY: $API_TOKEN" \
     -H 'Content-Type: application/json' \
     --data-binary @new_market_list.json   # 文件内容 = JSON 字符串，即逗号分隔的完整列表
```

> 这一步同时解决了「插件写进容器层、重建就丢」的问题 ——
> 通过市场安装的插件由 MP 自己下载管理，升级容器不受影响。

### 4. GITHUB_TOKEN（可选，只影响限流）

MP 安装插件时会调 `api.github.com`（未认证 **60 次/小时**，
认证后 5000 次/小时）。装上几个插件后容易撞限流。

到 GitHub → Settings → Developer settings → Personal access tokens，
**这个 token 不需要任何权限**（只读公开仓库即可，scope 留空就行），然后填到
`GITHUB_TOKEN`。它是给 MP **读**市场用的，跟 push 代码是两回事，
别指望它能把代码推上去。

私有仓库用 `REPO_GITHUB_TOKEN`，支持按仓库指定：`<owner>/<repo>:<token>,...`。

国内网络下 GitHub 直连不稳，MP 走 `PROXY_HOST`。**实测**本机
`PROXY_HOST`（例如 `http://<代理地址>:7890`，常见做法是本地跑一个 mihomo/clash），容器内即可正常拉到
`raw.githubusercontent.com`，无需额外配置。

### 5. 安装插件

保存市场源后，进 MoviePilot → 插件市场，应该能看到「硬链接与识别校验」，
点安装即可（列表已刷新则不用重启）。装完在插件配置页打开「启用」，
设好周期（默认每天 05:00），想立刻体检就先勾「立即运行一次」。

---

## 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| 启用插件 | 关 | 总开关，关闭时不注册定时任务 |
| 异常时通知 | 开 | 只在有 error/warn 时推送；提示项不发 |
| 立即运行一次 | 关 | 勾选保存后约 3 秒执行一次全量巡检 |
| 校验硬链接 | 开 | L1，纯本地 |
| 校验识别 | 开 | L2，纯本地 |
| 深度复核(TMDB) | 开 | L3，消耗 TMDB 请求，受「深度复核上限」约束 |
| 定时执行周期 | `0 5 * * *` | 5 位 cron |
| 判定回溯天数 | 3 | 只判定最近 N 天的整理记录 |
| 单次读取上限 | 3000 | 一次最多读多少条记录（含建立覆盖索引用的额外 14 天） |
| 深度复核上限 | 300 | 单次最多做多少次 TMDB 深复核 |
| 热度下限 | 10 | vote_count 低于此值视为脏词条特征 |
| 自动补链 | **关** | 把媒体库里的独立副本按 inode 换回硬链接以释放空间 |
| 单轮补链上限 | 50 | 每轮最多补几条 |
| 自动清理僵尸记录 | **关** | 删除「源与目标都已不存在」的 orphan `transferhistory` 记录 |
| 单轮清理上限 | 50 | 每轮最多清几条 |

### 「自动清理僵尸记录」到底删了什么

清理插件（`清理媒体文件` / `RemoveLink` 等）在源文件被删后会连带删掉媒体库硬链接、
刮削产物与空目录，**但它不会删 `transferhistory` 那一行** —— 于是记录一直指向
已不存在的路径，越积越脏。

开启本项后，插件会把这些记录从整理记录里移除。**只删数据库里的一行文本，
磁盘上没有任何文件被删除**（已核实调用链：`TransferHistoryOper.delete(rid)`
→ `TransferHistory.delete(db, rid)` → `Base.delete()` = 一条 `DELETE` SQL，
不触发任何 `StorageChain` / ORM 事件钩子，不碰磁盘）。

护栏（全部满足才动手）：

1. 开关打开，且未超出单轮上限；
2. 不是「被更新记录接管」的旧记录（superseded 的记录保留，留作追溯）；
3. 源文件与目标文件**确实都不存在**，且它们所在的卷**都已挂载进容器**
   （防止把「容器看不见」当成「文件不存在」）；
4. 删除前把整行记录完整快照写进插件数据 `prune_log`（保留最近 10 轮），
   随时可翻查、可人工回填。

## 输出分级

| 级别 | 判定项 | 是否通知 |
|---|---|---|
| **严重** | `dest_missing` 目标文件不存在（且无更新记录接管）<br>`link_broken` 硬链接已断开<br>`tmdbid_mismatch` 记录与识别结果 ID 冲突<br>`season_overflow` 季号超出该剧总季数<br>`tmdbid_tag_mismatch` 文件名 ID 标签与记录不符 | ✅ |
| **可疑** | `dirty_entry` TMDB 条目热度极低<br>`title_not_in_aliases` 记录标题不属于该条目别名<br>`year_conflict` 年份不一致（**且**已被深复核确认有问题） | ✅ |
| **提示** | `missing_media_id`、`recognize_failed`、`thin_aliases`、`category_mismatch`、`src_missing`、未确认的 `year_conflict`、`year_delta_ok`（公映年口径差异且 TMDB 同名条目唯一） | ❌ 折叠在详情页 |
| **已处理（仅计数）** | `superseded` 被更新的同路径记录接管<br>`skip_unmounted` 所在卷未挂载进容器<br>`cleaned` 源与目标整块被清理插件删除<br>`pruned` 本轮刚清掉的僵尸整理记录<br>`relinked` 本轮刚补好的硬链接 | ❌ 折叠在详情页 |

## 已知限制

- **只有挂载进 MoviePilot 容器的卷才能做 inode 比对。** MP 的 compose 里通常只挂了部分
  存储卷；未挂载卷下的记录会被标记为「未挂载」并跳过，不会误报为丢失。
  如果你希望覆盖全部卷，把这些卷也挂进 MP 容器即可。
  挂载状态由容器内 `/proc/mounts` 判定（v1.5.1 起），不依赖任何 NAS 厂商的目录命名约定。
- `dest_missing` 无法区分「被删除」和「被移动到未挂载的卷」，判定说明里已经写明。
- 深度复核对同一媒体有 TMDB 缓存，重复跑不会一直打请求，但首次全量体检会慢一些。
- 插件不是「自动修复器」——它只负责**发现并告警**。发现错配后如何修（重新整理 /
  加自定义识别词钉死 tmdbid）仍需人工判断，避免插件替你改坏数据。
- **「自动清理僵尸记录」的清理是分层收敛的。** 一批记录里可能存在「旧记录覆盖新记录」的
  关系链：只有当上层记录被清掉之后，下层那条才会浮出来变成「不再被接管」。所以第一次跑
  清 N 条、第二次还会再清一批，逐轮递减直到归零 —— 这是设计如此，不是异常。

---

## 发布到官方市场？

自建市场**零审核**，推上去就能用。如果想让更多人用，可以向官方市场提 PR：

- 仓库：`jxxghp/MoviePilot-Plugins`
- 需要在 `package.v2.json` 里注册你的插件，并按官方要求把插件源码放进
  该仓库的 `plugins/<id>/` 下（不是让你保留自己的仓库）
- 有代码规范与维护要求，审核周期不固定

建议先自建市场自己用一段时间，稳定后再考虑提 PR。

## License

MIT
