# -*- coding: utf-8 -*-
"""
HardlinkVerify —— MoviePilot 硬链接有效性 + 识别正确性 双重复核插件

== 为什么要这个插件 ==
MoviePilot 的 TMDB 匹配存在一类已知误配：当种子文件名里带着"季年份"
（例如 `Supernatural.2017.S13E09`）时，MP 会把 2017 当成首播年份去找剧集；
而 TMDB 上存在一些"首播年份刚好等于该年份、且带有同名译名"的低热度脏词条
（例如 261164 소능력자 / KR / 2017，vote_count=1），
`__search_tv_by_season()` 按 first_air_date 倒序取第一个命中 → 脏词条先于正确条目
（1622 邪恶力量 / 2005）被返回，于是文件被改名成外文剧名并归进错误分类目录。

本插件对整理记录做三层复核，把这类问题在"进库之后、发现之前"捞出来：

  L1 硬链接校验（离线）
      整理模式为 link 的记录，媒体库文件必须与下载目录源文件仍在同一 inode。
      断链 = 源种子删了之后媒体库还占着独立空间，或媒体库文件被换成了独立副本。

  L2 识别自洽性（离线，零外部请求）
      用当前识别词（含自定义识别词）重新解析源文件路径，与整理记录比对年份/季号。
      注意：只有"文件名里的季年份 ≠ 条目首播年"这一件事本身并不算错
      （多季剧普遍如此），所以 L2 的结论默认只是"待定"，
      必须由 L3 确认后才会上报，避免噪音。

  L3 深度复核（可选，走 MP 真实识别链 = TMDB）
      把源文件交给 MediaChain().recognize_media() 重新识别一次，比对：
        - tmdbid 是否一致          → 记录与识别器结论冲突（真正需要人看的东西）
        - vote_count 是否过低      → TMDB 脏词条特征
        - 别名数量是否过少         → 冷门/脏词条特征
        - 季号是否超过该剧总季数   → 强信号（脏词条常常只有 1 季）
        - 原产国与分类目录是否匹配 → 分类错位提示

== 关于误报抑制（重要） ==
真实环境里同一个媒体库路径会被反复整理覆盖：同一集的 1080p 与 2160p 版本会
hardlink 到**同一个 dest**，后一次会让前一次记录的 inode 关系失效。
这不是故障，是预期行为。因此插件会：
  - 遍历窗口外的记录建立"同路径 / 同剧同季 最新记录"索引
  - 只对**每个 dest 的最新那条记录**做判定，被覆盖的旧记录仅计数不计为异常
这样才能把信噪比压到可用水平（实测从 1311/2000 项噪音降到个位数真问题）。

误报抑制之二：**清理插件留下的孤儿记录**
「清理媒体文件 / RemoveLink」这类插件在下载文件被删除后，会按 inode 连带删除
媒体库中的硬链接、刮削文件（nfo/jpg/png）并清理空目录，但它**不删除
transferhistory 记录**。于是记录仍指向已不存在的路径，会被朴素实现误报成
"媒体库文件丢失"。本插件检测到「目标所在目录不存在 **且** 所有源文件都不存在」
时判定为「已清理」，归入提示而不计入异常。
（实测：某次 117 项异常里有 19 项属此类，逐条都能在 RemoveLink 日志里找到
 "立即删除硬链接文件" 的对应记录。）

误报抑制之三：**识别类校验的两条「口径豁免」**（v1.4.0 加入）
把 98 条识别类异常逐条追到 TMDB 查真值后发现，只有 1 个条目是真错
（记录把 2024 年的电影《Abigail / 噬血芭蕾》配成了委内瑞拉 1988 年的同名剧集），
其余 96 条全部是「匹配本来就对、只是判据不适用」：

  1. **多季剧按当季年份命名** —— 文件名写当季播出年（2026），记录年份写首播年（2020），
     这是多季剧的通用写法。豁免条件三条同时满足：记录季号 >= 2、文件年份 > 记录年份、
     目标路径已按 Season N 归档。
     （实测：《天赐的声音》S07、《擅长逃跑的殿下》S02 均属此类。）

  2. **中文内容低热度 ≠ 脏词条** —— 《天赐的声音》vote_count=2、《绝世战魂》vote_count=3，
     两个都是 TMDB 上的正确条目；反倒 vote_count=3279 的《惩罚者》(2004) 才是错配那条。
     综艺 / 国漫 / 国产剧 / 华语电影这几类中文内容在 TMDB 票数天然偏低，
     在这几类下低热度只作提示。其他分类（含「未分类」）仍照常告警 ——
     上面那条真错的《Abigail》正是落在「未分类」，所以照样被拎出来。

  3. **电影首映年 / 公映年差 1 年** —— 电影节先映、次年公映属常见情况，只对电影豁免。
     （实测：《上帝保佑美国》文件名 2011 / TMDB 发行日期 2012。）

另有一条**前置短路**：目标文件已不存在时直接跳过识别校验。
识别校验回答的是「这份内容配对了吗」，文件都没了这个问题就没有意义，
L1 已经就「文件没了」给出结论（superseded / cleaned / dest_missing）——
再拿一条指向空路径的记录去比对 TMDB 只会产出噪声。
（实测：98 条里有 10 条正是如此，其文件早已被清理插件删除。）

**这四条改动把实测的 98 条异常压到 2 条**，剩下的 2 条就是那个真错条目，
需要人去改数据 —— 这正是本插件该有的信噪比。

== 关于"自动修复"的边界（重要） ==
本插件默认**只发现、不改数据**。可选开关「自动补链」只处理唯一一种确定安全的情况：

  媒体库文件是**独立副本** —— 与源文件内容完全一致、只是 inode 不同，
  白占一份空间（同一集被 link 成两份实体）。

此时按 inode 重建硬链接即可省下一份空间。护栏（全部满足才动手）：
  - 该副本 st_nlink == 1（是"孤本"，不存在别的路径指向它 =
    不会被清理插件按 inode 连带删除其它文件）
  - 源文件存在、同文件系统、**字节数完全一致**（避免把 2160p 降级成 1080p 的链接）
  - 先 os.link 建临时名并校验 inode，再 os.replace 原子替换；失败回滚

**明确不做的事**：不去"修复"「目标文件不存在」的记录。因为这类记录的源文件
通常也已随清理一起消失 —— 无物可链，唯一出路是重新下载；而重新下载会再次触发
清理插件（它监听到新增即纳管、源被删即连带清库），形成"下了又删"的死循环。

== 关于"僵尸记录清理"（可选，默认关闭） ==

清理插件（RemoveLink 等）在源文件被删后会连带删掉媒体库硬链接、刮削文件与空目录，
但它**不删 transferhistory 记录** —— 于是这类记录越攒越多，一直指向早已不存在的路径。
可选开关「自动清理僵尸整理记录」把这些残留从数据库里删掉。

判定"僵尸"的硬条件（源与目标都已不存在）：
  - srcs 非空、dest 非空；
  - dest 不存在，且 srcs 里**每一个**都不存在；
  - dest 与每个 src 所在卷都已挂载进 MP 容器（未挂载一律判为"看不见"，
    **绝不**当作"不存在" —— 容器的 os.path.exists 对没挂载的卷一律返回 False）。
被更新记录接管的旧记录（superseded）保留不删，留作追溯。

安全性：`TransferHistoryOper().delete(id)` 最终落到
`Base.delete()` = `db.query(cls).filter(id == rid).delete()`，
**纯数据库行删除，不触碰磁盘**。做种源文件、媒体库母本、刮削产物全部不受影响。
每条被删记录都会把整行快照写进插件数据 `prune_log`（保留最近 10 轮），可翻查/可恢复。
"""

import datetime
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.metainfo import MetaInfoPath
from app.db.transferhistory_oper import TransferHistoryOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType

# 国家/地区 → 二级分类关键词
_REGION_MAP = {
    "日韩": ("JP", "KR", "KP"),
    "欧美": ("US", "GB", "FR", "DE", "ES", "IT", "CA", "AU", "NZ",
             "SE", "DK", "NO", "FI", "NL", "BE", "IE", "RU", "MX", "BR", "AR"),
    "华语": ("CN", "TW", "HK", "MO", "SG", "MY"),
    "印": ("IN", "PK", "BD"),
    "泰": ("TH",),
}

_LEVEL_ORDER = {"error": 0, "warn": 1, "info": 2}
_LEVEL_TEXT = {"error": "严重", "warn": "可疑", "info": "提示"}
# 只有这两个级别计入"异常"并触发通知；info 只作为提示保留在详情页
_ACTIONABLE_LEVELS = {"error", "warn"}

# 建立"被覆盖"索引时额外回看的天数
_LOOKBACK_EXTRA_DAYS = 14

# 这些分类下 TMDB 票数天然偏低，不能据此判定"脏词条"（见误报抑制之三）
_LOW_VOTE_CATEGORIES = ("综艺", "国漫", "国产剧", "华语电影")


class HardlinkVerify(_PluginBase):
    # ---- 插件元信息 ----
    plugin_name = "硬链接与识别校验"
    plugin_desc = ("巡检已整理媒体：① 校验硬链接是否仍然有效（源种子还在不在）；"
                   "② 复核识别结果是否自洽（重新解析源文件名比对年份/标题）；"
                   "③ 可选深度复核（走 TMDB 重新识别，比对 tmdbid、热度、季数），"
                   "把「美剧被刮成日韩剧」这类误配在进库后捞出来并告警。"
                   "已内置四类误报抑制：同路径覆盖、清理插件（RemoveLink 等）留下的孤儿记录、"
                   "多季剧「当季年份」命名口径、中文内容低热度条目。"
                   "可选「自动补链」：把媒体库里的独立副本按 inode 换回硬链接以释放空间，默认关闭。"
                   "可选「自动清理僵尸整理记录」：源与目标都已不存在的残留记录（清理插件不会删它们）"
                   "从数据库里清掉，只删记录不删文件，默认关闭。")
    plugin_version = "1.5.0"
    plugin_author = "spizmm"
    plugin_icon = "https://raw.githubusercontent.com/yuez414-eng/MoviePilot-Plugins/main/icons/hardlinkverify.png"
    author_url = ""
    plugin_config_prefix = "HardlinkVerify_"
    plugin_order = 50
    auth_level = 1

    # ---- 运行期状态 ----
    _enable: bool = False
    _notify: bool = True
    _onlyonce: bool = False
    _cron: str = "0 5 * * *"
    _days: int = 3
    _check_link: bool = True
    _check_recognize: bool = True
    _deep: bool = True
    _max_deep: int = 300
    _min_vote: int = 10
    _min_alias: int = 3
    _max_records: int = 3000
    _auto_relink: bool = False
    _relink_max: int = 50
    _relink_used: int = 0
    _auto_prune: bool = False
    _prune_max: int = 50
    _prune_used: int = 0
    _prune_stamp: str = ""
    _scheduler: Optional[BackgroundScheduler] = None

    # ==================================================================
    # 生命周期
    # ==================================================================
    def init_plugin(self, config: dict = None):
        self.stop_service()
        config = config or {}
        self._enable = bool(config.get("enable", False))
        self._notify = bool(config.get("notify", True))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._cron = (config.get("cron") or "0 5 * * *").strip()
        self._days = self.__to_int(config.get("days"), 3)
        self._check_link = bool(config.get("check_link", True))
        self._check_recognize = bool(config.get("check_recognize", True))
        self._deep = bool(config.get("deep", True))
        self._max_deep = self.__to_int(config.get("max_deep"), 300)
        self._min_vote = self.__to_int(config.get("min_vote"), 10)
        self._min_alias = self.__to_int(config.get("min_alias"), 3)
        self._max_records = self.__to_int(config.get("max_records"), 3000)
        self._auto_relink = bool(config.get("auto_relink", False))
        self._relink_max = self.__to_int(config.get("relink_max"), 50)
        self._auto_prune = bool(config.get("auto_prune", False))
        self._prune_max = self.__to_int(config.get("prune_max"), 50)

        if self._onlyonce:
            logger.info("【硬链接与识别校验】立即运行一次")
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.__task,
                trigger="date",
                run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ))
                + datetime.timedelta(seconds=3),
            )
            self._onlyonce = False
            self.__update_config()
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enable

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/run",
                "endpoint": self.__api_run,
                "methods": ["POST"],
                "summary": "立即执行一次巡检",
                "description": "手动触发硬链接与识别复核，结果写入插件详情页",
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enable and self._cron:
            try:
                trigger = CronTrigger.from_crontab(self._cron)
            except Exception as e:
                logger.error(f"【硬链接与识别校验】cron 表达式无效：{self._cron} - {e}")
                return []
            return [
                {
                    "id": "HardlinkVerify",
                    "name": "硬链接与识别巡检",
                    "trigger": trigger,
                    "func": self.__task,
                    "kwargs": {},
                }
            ]
        return []

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"【硬链接与识别校验】停止服务失败：{e}")

    # ==================================================================
    # 手动触发 API
    # ==================================================================
    def __api_run(self) -> dict:
        try:
            summary = self.__task()
            return {"success": True, "message": "巡检完成", "data": summary}
        except Exception as e:
            logger.error(f"【硬链接与识别校验】手动巡检失败：{e}")
            return {"success": False, "message": str(e)}

    # ==================================================================
    # 主流程
    # ==================================================================
    def __task(self) -> dict:
        started = datetime.datetime.now(tz=pytz.timezone(settings.TZ))
        tz = pytz.timezone(settings.TZ)
        today = datetime.datetime.now(tz=tz).date()
        cutoff = today - datetime.timedelta(days=max(self._days, 1) - 1)

        logger.info(f"【硬链接与识别校验】开始巡检，判定范围：{cutoff} 起")

        # 回看更多天，用来建立"被覆盖"索引
        try:
            all_records = self.__collect_records(
                max(self._days, 1) + _LOOKBACK_EXTRA_DAYS, cap=self._max_records
            )
        except Exception as e:
            logger.error(f"【硬链接与识别校验】读取整理记录失败：{e}")
            return {"checked": 0, "issues": 0, "error": str(e)}

        # 同 dest / 同剧同季 的最新记录 id
        dest_latest: Dict[str, int] = {}
        key_latest: Dict[Tuple, int] = {}
        for r in all_records:
            rid = getattr(r, "id", None)
            if not rid:
                continue
            d = getattr(r, "dest", None)
            if d:
                dest_latest[d] = max(dest_latest.get(d, 0), rid)
            k = (getattr(r, "tmdbid", None), getattr(r, "seasons", None))
            if k[0]:
                key_latest[k] = max(key_latest.get(k, 0), rid)

        # 只判定窗口内的记录
        targets = []
        for r in all_records:
            dt = self.__parse_dt(getattr(r, "date", ""))
            if dt is None or dt.date() >= cutoff:
                targets.append(r)

        logger.info(f"【硬链接与识别校验】索引 {len(all_records)} 条，待判定 {len(targets)} 条")

        issues: List[dict] = []
        notes: List[dict] = []
        deep_budget = self._max_deep
        self._relink_used = 0
        self._prune_used = 0
        self._prune_stamp = started.strftime("%Y-%m-%d %H:%M:%S")
        stat = {"checked": 0, "link_bad": 0, "reco_bad": 0,
                "skipped_unmounted": 0, "superseded": 0, "deep_done": 0,
                "cleaned": 0, "relinked": 0, "pruned": 0}

        ctx = {"dest_latest": dest_latest, "key_latest": key_latest, "stat": stat}

        for rec in targets:
            stat["checked"] += 1
            try:
                rec_issues, deep_used = self.__check_record(rec, deep_budget, ctx)
            except Exception as e:
                logger.error(f"【硬链接与识别校验】记录 #{getattr(rec, 'id', '?')} 复核异常：{e}")
                continue
            deep_budget -= deep_used
            stat["deep_done"] += deep_used
            for it in rec_issues:
                if it["kind"] == "skip_unmounted":
                    stat["skipped_unmounted"] += 1
                    continue
                if it.get("kind") == "superseded":
                    stat["superseded"] += 1
                    continue
                if it.get("kind") == "cleaned":
                    # 源与目标整块已被清理插件删除 → 预期行为，只作提示
                    stat["cleaned"] += 1
                    notes.append(it)
                    continue
                if it.get("kind") == "pruned":
                    # 本轮刚把僵尸整理记录从数据库里清掉 → 已处理完，只作提示
                    stat["pruned"] += 1
                    notes.append(it)
                    continue
                if it.get("kind") == "relinked":
                    # 本轮刚把独立副本换回硬链接 → 已处理完，只作提示
                    stat["relinked"] += 1
                    notes.append(it)
                    continue
                if it["level"] not in _ACTIONABLE_LEVELS:
                    notes.append(it)
                    continue
                issues.append(it)
                if it["group"] == "link":
                    stat["link_bad"] += 1
                else:
                    stat["reco_bad"] += 1

        issues.sort(key=lambda x: (_LEVEL_ORDER.get(x["level"], 9), -int(x.get("id") or 0)))
        notes.sort(key=lambda x: (_LEVEL_ORDER.get(x["level"], 9), -int(x.get("id") or 0)))

        result = {
            "time": started.strftime("%Y-%m-%d %H:%M:%S"),
            "range_days": self._days,
            "checked": stat["checked"],
            "issues": len(issues),
            "notes": len(notes),
            "link_bad": stat["link_bad"],
            "reco_bad": stat["reco_bad"],
            "unmounted": stat["skipped_unmounted"],
            "superseded": stat["superseded"],
            "deep_done": stat["deep_done"],
            "cleaned": stat["cleaned"],
            "relinked": stat["relinked"],
            "pruned": stat["pruned"],
            "items": issues[:400],
            "note_items": notes[:200],
        }
        history = self.get_data("history") or []
        history = [result] + [h for h in history if isinstance(h, dict)][:19]
        self.save_data("history", history)
        self.save_data("last", result)

        logger.info(
            f"【硬链接与识别校验】巡检完成：判定 {result['checked']} 条，"
            f"异常 {result['issues']} 条（断链 {result['link_bad']} / 识别 {result['reco_bad']}），"
            f"提示 {result['notes']} 条（其中已清理 {result['cleaned']} / 已补链 {result['relinked']} "
            f"/ 已删僵尸记录 {result['pruned']}），"
            f"被覆盖跳过 {result['superseded']} 条，"
            f"深度复核 {result['deep_done']} 条，未挂载跳过 {result['unmounted']} 条"
        )
        self.__notify_result(result)
        return result

    # ==================================================================
    # 逐条复核
    # ==================================================================
    def __check_record(self, rec, deep_budget: int, ctx: dict) -> Tuple[List[dict], int]:
        found: List[dict] = []
        pending: List[dict] = []          # 需 L3 确认后才上报的弱信号
        base = {
            "id": getattr(rec, "id", None),
            "date": getattr(rec, "date", "") or "",
            "title": getattr(rec, "title", "") or "",
            "year": getattr(rec, "year", "") or "",
            "tmdbid": getattr(rec, "tmdbid", None),
            "doubanid": getattr(rec, "doubanid", None),
            "bangumiid": getattr(rec, "bangumiid", None),
            "anilistid": getattr(rec, "anilistid", None),
            "category": getattr(rec, "category", "") or "",
            "seasons": getattr(rec, "seasons", "") or "",
            "mode": getattr(rec, "mode", "") or "",
            "src": getattr(rec, "src", "") or "",
            "dest": getattr(rec, "dest", "") or "",
        }

        src, dest = base["src"], base["dest"]

        if src and not self.__is_mounted(src):
            return [dict(base, group="skip", kind="skip_unmounted", level="info",
                         reason="路径未挂载进容器",
                         detail=f"该记录所在卷未挂载进 MoviePilot 容器，跳过：{self.__root_of(src)}")], 0

        # 这条记录是否已被后续整理覆盖（同路径 or 同剧同季的更新记录）
        superseded = self.__superseded_by(base, ctx)

        # ---------- L1 硬链接 ----------
        if self._check_link and dest:
            found.extend(self.__check_link(rec, base, superseded))

        # ---------- L2 + L3 识别 ----------
        # 识别校验回答的是「这份内容配对了吗」。目标文件已经不在了，这个问题就没有意义 ——
        # L1 已经就「文件没了」给出结论（superseded / cleaned / dest_missing），
        # 再拿一条指向空路径的记录去比对 TMDB，只会产出噪声。
        # （实测：98 条识别类异常里有 10 条属于此类 —— 记录的文件早已被清理，
        #  却仍被 L2/L3 拎出来报「年份冲突 / tmdbid 不一致」。）
        deep_used = 0
        content_gone = bool(dest) and not os.path.exists(dest)
        if self._check_recognize and src and not content_gone:
            l2, need_deep, meta = self.__check_recognize_offline(rec, base)
            for it in l2:
                (pending if it.pop("_pending", False) else found).append(it)

            if self._deep and (need_deep or pending) and deep_budget > 0:
                deep_used = 1
                deep_issues = self.__check_recognize_deep(rec, base, meta)
                found.extend(deep_issues)
                if deep_issues:
                    # 深复核确认有问题 → 一并保留命名风险提示
                    for it in pending:
                        it["level"] = "warn"
                        found.append(it)
                elif not pending:
                    pass
            else:
                # 没走深复核，弱信号降级为提示（保留透明度）
                for it in pending:
                    it["level"] = "info"
                    it["reason"] = "命名风险（未做深度复核）：" + it["reason"]
                    found.append(it)

        return found, deep_used

    def __superseded_by(self, base: dict, ctx: dict) -> Optional[str]:
        rid = base["id"] or 0
        if not rid:
            return None
        d = base["dest"]
        if d:
            newer = ctx["dest_latest"].get(d, 0)
            if newer > rid:
                return f"同路径已被更新的整理记录 #{newer} 覆盖"
        if base["tmdbid"]:
            newer = ctx["key_latest"].get((base["tmdbid"], base["seasons"]), 0)
            if newer > rid:
                return f"同剧同季已被更新的整理记录 #{newer} 覆盖"
        return None

    # ---------------------------- L1 ----------------------------
    def __check_link(self, rec, base: dict, superseded: Optional[str]) -> List[dict]:
        srcs = rec.files if isinstance(getattr(rec, "files", None), list) and rec.files else []
        if not srcs and base["src"]:
            srcs = [base["src"]]
        srcs = [s for s in srcs if isinstance(s, str) and s]
        mode = (base["mode"] or "").lower()
        dest = base["dest"]
        out: List[dict] = []

        def emit(kind: str, level: str, reason: str, detail: str):
            out.append(dict(base, group="link", kind=kind, level=level,
                            reason=reason, detail=detail))

        if not os.path.exists(dest):
            if superseded:
                # 旧记录的目标已被新记录接管 → 预期行为，不计异常
                emit("superseded", "info", superseded, superseded)
                return out
            # 记录已成"僵尸"（源与目标都不存在了）→ 可选清理，详见 __try_prune
            pruned = self.__try_prune(rec, base, srcs, dest)
            if pruned:
                out.append(pruned)
                return out
            if self.__looks_cleaned(srcs, dest):
                # 源与目标整块消失 → 清理插件（RemoveLink 等）删种同时删库的预期结果
                emit("cleaned", "info", "整块资源已被清理",
                     "下载文件与媒体库目录均已不存在，符合清理插件"
                     "（如「清理媒体文件 / RemoveLink」在源文件被删后连带清理硬链接、"
                     "刮削文件并删除空目录）的预期行为；该类清理不删除整理记录，"
                     "因此记录仍会指向已不存在的路径。不计为异常。")
            else:
                emit("dest_missing", "error", "媒体库文件不存在",
                     f"整理记录指向的目标文件已不存在，且没有更新的整理记录接管该路径。"
                     f"可能是被误删/被清理插件删除、媒体库改过目录结构，"
                     f"或文件被移到了未挂载进容器的卷。目标：{dest}")
            return out

        if mode not in ("link", "hardlink"):
            return out

        # 收集目标侧 inode
        target_inodes = set()
        try:
            dest_path = Path(dest)
            if dest_path.is_file():
                st = os.stat(dest_path)
                target_inodes.add((st.st_dev, st.st_ino))
            else:
                for f in dest_path.rglob("*"):
                    if f.is_file():
                        st = os.stat(f)
                        target_inodes.add((st.st_dev, st.st_ino))
        except OSError as e:
            logger.warning(f"【硬链接与识别校验】读取目标路径失败 {dest}：{e}")

        lost_src, broken = [], []
        for s in srcs:
            if not os.path.exists(s):
                lost_src.append(s)
                continue
            try:
                st = os.stat(s)
            except OSError:
                lost_src.append(s)
                continue
            if (st.st_dev, st.st_ino) not in target_inodes:
                broken.append(s)

        if broken:
            if superseded:
                emit("superseded", "info", superseded,
                     f"{len(broken)} 个源文件与目标不再同 inode —— {superseded}")
            else:
                fixed = self.__try_relink(broken, base, dest)
                if fixed:
                    out.append(fixed)
                else:
                    emit("link_broken", "error", "硬链接已断开",
                         f"{len(broken)} 个源文件在媒体库中没有对应的硬链接"
                         f"（媒体库成了独立副本：源种子删掉后仍占空间，且不再是保种文件）。"
                         f"示例：{os.path.basename(broken[0])}")
        if lost_src:
            emit("src_missing", "info", "源文件已不在下载目录",
                 f"{len(lost_src)} 个源文件已不存在（做种结束正常清理可忽略；"
                 f"媒体库硬链接本身仍指向文件本体，不会丢）。"
                 f"示例：{os.path.basename(lost_src[0])}")
        return out

    # ---------------------- 已清理 / 自动补链 ----------------------
    @staticmethod
    def __looks_cleaned(srcs: List[str], dest: str) -> bool:
        """
        判定「整块资源已被清理」。

        典型场景：清理插件（「清理媒体文件 / RemoveLink」）监控到下载文件被删除后，
        会按 inode 连带删除媒体库中的硬链接、刮削文件（nfo/jpg/png）并清理空目录，
        但它不删除 transferhistory 记录 —— 于是记录仍指向已不存在的路径。
        这类记录不是故障，不应计入异常。

        判据（三者同时满足才算"已清理"）：
          1. 目标所在目录也不存在（整块消失，而非单个文件丢失）；
          2. 有源文件信息（信息不足时保守判为异常 —— 宁可多报，不可漏报）；
          3. 所有源文件都不存在（源也没了 → 是整体清理，不是媒体库被误删）。
        """
        if not srcs:
            return False
        parent = os.path.dirname(dest)
        if parent and os.path.exists(parent):
            return False
        return not any(os.path.exists(s) for s in srcs)

    # ---------------------- 僵尸记录清理（可选） ----------------------
    @classmethod
    def __is_prunable(cls, srcs: List[str], dest: str) -> bool:
        """
        判定一条整理记录是否已成"僵尸记录"：**源文件与目标文件都不存在了**。

        与 __looks_cleaned 的区别：后者额外要求"目标所在目录也不存在"
        （严格意义上的"整块被清理"）；本方法只要目标文件本身没了、且源文件
        全部没了就算。差异场景：剧集目录里其他集还在，只有这一集被清理插件
        删掉 —— 落进 dest_missing 被当成"媒体库文件丢失"误报，其实同属僵尸。

        安全前提（缺一不可，否则一律判为"不可清理"）：
          1. 有源文件信息、有目标路径；
          2. 目标所在卷**已挂载进 MoviePilot 容器**；
          3. 每个源文件所在卷也都已挂载。
        第 2、3 条是硬护栏：容器的 os.path.exists 对"没挂载的卷"一律返回 False，
        若不先确认挂载状态，就会把"看不见"当成"不存在"，误删活记录。
        """
        if not srcs or not dest:
            return False
        if not cls.__is_mounted(dest):
            return False
        for s in srcs:
            if not cls.__is_mounted(s):
                return False
            if os.path.exists(s):
                return False
        return True

    def __try_prune(self, rec, base: dict, srcs: List[str], dest: str) -> Optional[dict]:
        """
        删除「源与目标都已不存在」的僵尸整理记录（可选功能，默认关闭）。

        为什么这么做是安全的：这条记录指向的下载文件与媒体库文件都已经不存在了，
        清理掉的只是一行数据库文本 —— 磁盘上没有任何东西随之消失，做种源文件、
        媒体库母本、刮削产物全部不受影响。这类记录是清理插件（RemoveLink 等）
        删种删库时**不会**顺手删掉的残留，积累下去会让整理记录页越来越脏。

        护栏（全部满足才动手）：
          1. 开关 self._auto_prune 开启，且未超出本轮 self._prune_max 上限；
          2. 非「被更新记录接管」（superseded 的旧记录保留，留作追溯）；
          3. __is_prunable：源与目标确实都不存在，且相关卷均已挂载进容器；
          4. 删除前把整行记录快照写进插件数据 prune_log（保留最近 10 轮），
             随时可翻查、可人工恢复。
        """
        if not self._auto_prune or self._prune_used >= max(self._prune_max, 0):
            return None
        rid = base.get("id")
        if not rid or not self.__is_prunable(srcs, dest):
            return None

        snapshot, detail = self.__snapshot_record(rec, base, srcs, dest)
        try:
            TransferHistoryOper().delete(int(rid))
        except Exception as e:
            logger.error(f"【硬链接与识别校验】清理僵尸记录 #{rid} 失败：{e}")
            return None

        self._prune_used += 1
        self.__record_prune(detail)
        logger.info(
            f"【硬链接与识别校验】已清理僵尸整理记录 #{rid}"
            f"《{base.get('title')}》→ {dest}"
        )
        return dict(base, group="link", kind="pruned", level="info",
                    reason="僵尸整理记录已清理",
                    detail=("该记录指向的下载文件与媒体库文件都已不存在，"
                            "记录本身已无用途，已从整理记录中移除（**只删记录，"
                            "磁盘上没有任何文件被删除**）。"
                            "已留快照可查：插件数据 prune_log。"
                            f"目标：{dest}"))

    def __record_prune(self, snapshot: dict):
        """把清理快照按轮次追加进插件数据 prune_log（保留最近 10 轮）。"""
        try:
            log = self.get_data("prune_log") or []
            if not isinstance(log, list):
                log = []
            stamp = self._prune_stamp or datetime.datetime.now(
                tz=pytz.timezone(settings.TZ)
            ).strftime("%Y-%m-%d %H:%M:%S")
            if log and isinstance(log[0], dict) and log[0].get("time") == stamp:
                log[0].setdefault("items", []).append(snapshot)
            else:
                log.insert(0, {"time": stamp, "items": [snapshot]})
            self.save_data("prune_log", log[:10])
        except Exception as e:
            # 快照写失败不阻断清理本身，但要在日志里留痕
            logger.warning(f"【硬链接与识别校验】写入清理快照失败：{e}")

    @staticmethod
    def __snapshot_record(rec, base: dict, srcs: List[str], dest: str) -> Tuple[str, dict]:
        """生成被清理记录的完整快照，用于事后追溯/恢复。"""
        row = {}
        try:
            for col in rec.__table__.columns:
                row[col.name] = getattr(rec, col.name, None)
        except Exception:
            row = {"id": base.get("id"), "title": base.get("title")}
        row["files"] = srcs
        text = json.dumps(row, ensure_ascii=False, default=str)
        entry = {
            "id": base.get("id"),
            "date": base.get("date"),
            "title": base.get("title"),
            "year": base.get("year"),
            "tmdbid": base.get("tmdbid"),
            "category": base.get("category"),
            "seasons": base.get("seasons"),
            "mode": base.get("mode"),
            "dest": dest,
            "src": base.get("src"),
            "row": text[:20000],
        }
        return text, entry

    def __try_relink(self, srcs: List[str], base: dict, dest: str) -> Optional[dict]:
        """
        把「媒体库独立副本」换回硬链接（可选功能，默认关闭）。

        安全护栏 —— 全部满足才动手，任一不满足即放弃并保持原判定：
          1. 开关 self._auto_relink 打开，且未超出本轮 self._relink_max 上限；
          2. dest 是文件，且 **st_nlink == 1**。这条最关键：被替换掉的那份必须是
             "孤本"，不存在其他硬链接指向它。因为清理插件是按 inode 工作的
             （删除时会找出同 inode 的所有路径一并删除），若 dest 还被别的文件
             引用着，替换会连带把那个文件（很可能是正在做种的源）删掉；
          3. 存在一个源文件：同文件系统 + **字节数完全相同**。内容一致才换，
             避免把 2160p 的库文件降级成 1080p 的链接；
          4. 先 os.link 到临时名并校验 inode，再 os.replace 原子替换；
             任何一步失败都干净退出，绝不动原文件。
        """
        if not self._auto_relink or self._relink_used >= max(self._relink_max, 0):
            return None
        try:
            if not os.path.isfile(dest):
                return None
            dst = os.stat(dest)
        except OSError:
            return None
        if dst.st_nlink != 1:
            return None

        for s in srcs:
            tmp = ""
            try:
                if not os.path.isfile(s):
                    continue
                sst = os.stat(s)
                if sst.st_dev != dst.st_dev:
                    continue
                if os.path.getsize(s) != dst.st_size:
                    continue
                tmp = dest + ".hlvfix"
                if os.path.exists(tmp):
                    os.remove(tmp)
                os.link(s, tmp)
                tst = os.stat(tmp)
                if (tst.st_dev, tst.st_ino) != (sst.st_dev, sst.st_ino):
                    os.remove(tmp)
                    continue
                os.replace(tmp, dest)
                nst = os.stat(dest)
            except OSError as e:
                logger.warning(f"【硬链接与识别校验】自动补链失败 {dest}：{e}")
                if tmp and os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                return None

            if (nst.st_dev, nst.st_ino) != (sst.st_dev, sst.st_ino):
                return None

            self._relink_used += 1
            logger.info(f"【硬链接与识别校验】已自动补链：{dest} <- {s}")
            return dict(base, group="link", kind="relinked", level="info",
                        reason="独立副本已换回硬链接",
                        detail=f"该路径原为独立副本（与源内容一致、仅 inode 不同，"
                               f"白占一份空间）。已重建为硬链接，释放约 "
                               f"{dst.st_size / 1048576:.0f} MB。源：{os.path.basename(s)}")
        return None

    # ---------------------------- L2 ----------------------------
    def __check_recognize_offline(self, rec, base: dict) -> Tuple[List[dict], bool, Any]:
        """离线识别自洽性检查。返回 (问题列表, 是否需要深复核, meta)。"""
        out: List[dict] = []
        need_deep = False

        try:
            meta = MetaInfoPath(Path(base["src"]))
        except Exception as e:
            logger.warning(f"【硬链接与识别校验】解析源路径失败 {base['src']}：{e}")
            return out, False, None

        parsed_year = str(meta.year) if getattr(meta, "year", None) else ""
        rec_year = str(base["year"]) if base["year"] else ""

        # ① 年份不一致 —— 是"容易被误配"的命名模式，本身不一定是错（多季剧普遍如此）
        if parsed_year and rec_year and parsed_year != rec_year:
            benign = self.__benign_year_delta(base, parsed_year, rec_year)
            if benign:
                # 口径差异已经能解释清楚 → 只作提示，不占异常数
                out.append(dict(
                    base, group="reco", kind="year_delta_ok", level="info",
                    reason=benign[0], detail=benign[1],
                ))
            else:
                out.append(dict(
                    base, group="reco", kind="year_conflict", level="warn", _pending=True,
                    reason="文件名年份与条目年份不一致",
                    detail=(f"源文件名解析出的年份是 {parsed_year}，整理记录里的条目是"
                            f"《{base['title']}》({rec_year})。多季剧把「当季年份」写进文件名"
                            f"是常见做法，本身不算错；但正是这种命名会让 MP 匹配到"
                            f"同名的低热度词条，所以需要深复核确认。"),
                ))
                need_deep = True

        # ② 文件名里有显式 tmdbid 标签 → 可直接判定
        tagged = getattr(meta, "tmdbid", None)
        if tagged and base["tmdbid"] and int(tagged) != int(base["tmdbid"]):
            out.append(dict(
                base, group="reco", kind="tmdbid_tag_mismatch", level="error",
                reason="ID 标签与整理记录不一致",
                detail=(f"源文件名里显式标注 tmdbid={tagged}，"
                        f"但整理记录写的是 tmdbid={base['tmdbid']}。"),
            ))

        # ③ 四个数据源 ID 全空 → 识别链路可疑（仅提示，综艺/动漫常见）
        if not any([base["tmdbid"], base["doubanid"], base["bangumiid"], base["anilistid"]]):
            out.append(dict(
                base, group="reco", kind="missing_media_id", level="info",
                reason="整理记录没有任何媒体 ID",
                detail="这条成功记录里 tmdbid/豆瓣/Bangumi/AniList ID 全为空，"
                       "识别链路可能只靠文件名，建议留意。",
            ))

        # ④ 源文件名季号 vs 记录季号（弱信号，待确认）
        try:
            begin_season = getattr(meta, "begin_season", None)
            rec_season = self.__parse_season(base["seasons"])
            if begin_season and rec_season and int(begin_season) != rec_season:
                out.append(dict(
                    base, group="reco", kind="season_conflict", level="warn", _pending=True,
                    reason="季号不一致",
                    detail=(f"源文件名是 S{begin_season:02d}，"
                            f"整理记录写的是 {base['seasons']}。"),
                ))
                need_deep = True
        except Exception:
            pass

        return out, need_deep, meta

    def __benign_year_delta(self, base: dict, parsed_year: str, rec_year: str
                            ) -> Optional[Tuple[str, str]]:
        """年份口径差异豁免：能用「合理命名习惯」解释时返回 (原因, 说明)，否则 None。

        实测（2026-09-16）本库 50 条 year_conflict 里 45 条属于下面两种无害情形，
        真正的错配只有 1 条 —— 而那条记录的 seasons 是 S01（不是多季剧）、
        分类也不是电影语境，所以两个豁免都不会放过它。
        """
        try:
            py, ry = int(parsed_year), int(rec_year)
        except (TypeError, ValueError):
            return None

        season = self.__parse_season(base["seasons"])
        dest = base["dest"] or ""

        # 情形一：多季剧的「当季年份」。
        # 文件名写当季播出年、记录年份写首播年，且记录季号 >= 2、
        # 目标路径里也确实按 Season N 归档 → 完全自洽，不是错误。
        if season and season >= 2 and py > ry:
            if re.search(r"[Ss]eason[\s._-]*0*%d(?!\d)" % season, dest):
                return (
                    "多季剧按当季年份命名",
                    f"源文件名用的是当季播出年 {parsed_year}，条目《{base['title']}》的首播年是 "
                    f"{rec_year}，记录季号为 {base['seasons']}，目标路径也已按 "
                    f"Season {season} 归档 —— 这是多季剧通用的命名口径，不是识别错误。",
                )

        # 情形二：电影的首映年 / 公映年差异（电影节先映，次年才正式公映）。
        # 只对电影（记录里没有季号）生效，且年份差 <= 1。
        if not base["seasons"] and abs(py - ry) <= 1:
            return (
                "电影首映年与公映年差异",
                f"源文件名标注 {parsed_year}，条目《{base['title']}》在 TMDB 的发行日期是 "
                f"{rec_year} —— 电影节首映与正式公映跨年属常见情况，年份差 1 年不算错。",
            )

        return None

    # ---------------------------- L3 ----------------------------
    def __check_recognize_deep(self, rec, base: dict, meta) -> List[dict]:
        out: List[dict] = []
        if meta is None:
            return out

        try:
            from app.chain.media import MediaChain
            mediainfo = MediaChain().recognize_media(meta=meta)
        except Exception as e:
            logger.warning(f"【硬链接与识别校验】深度识别失败 #{base['id']}：{e}")
            return out

        if not mediainfo:
            out.append(dict(
                base, group="reco", kind="recognize_failed", level="info",
                reason="重新识别不到媒体信息",
                detail="按当前识别词重新识别源文件，TMDB 没有返回结果"
                       "（综艺/国漫等中文名资源较常见，不一定代表出错）。",
            ))
            return out

        alias_norms = {self.__norm(n) for n in (mediainfo.names or []) if n}
        alias_norms.add(self.__norm(mediainfo.title))
        alias_norms.add(self.__norm(mediainfo.original_title))
        alias_norms.discard("")
        rec_title_norm = self.__norm(base["title"])

        # ① tmdbid 冲突 —— 本插件最核心的输出
        if base["tmdbid"] and mediainfo.tmdb_id and int(mediainfo.tmdb_id) != int(base["tmdbid"]):
            out.append(dict(
                base, group="reco", kind="tmdbid_mismatch", level="error",
                reason="记录与识别结果 TMDB ID 不一致",
                detail=(f"整理记录写的是 tmdbid={base['tmdbid']}"
                        f"（《{base['title']}》"
                        f"{' (' + base['year'] + ')' if base['year'] else ''}），"
                        f"而按当前识别词重新识别得到 tmdbid={mediainfo.tmdb_id}"
                        f"（《{mediainfo.title}》"
                        f"{' (' + str(mediainfo.year) + ')' if mediainfo.year else ''}）。"
                        f"两者必有其一不对：要么这条记录当时识别错了，"
                        f"要么识别器至今仍会被同名脏词条带偏。"),
            ))

        # ② 记录标题不在该条目的任何别名里
        if rec_title_norm and alias_norms and not any(
                rec_title_norm in a or a in rec_title_norm for a in alias_norms):
            out.append(dict(
                base, group="reco", kind="title_not_in_aliases", level="warn",
                reason="记录标题不属于该条目的别名",
                detail=(f"记录标题《{base['title']}》不在 tmdbid={mediainfo.tmdb_id}"
                        f"（《{mediainfo.title}》）的任何已知别名里。"),
            ))

        # ③ 季号越界 —— 脏词条常常只有 1 季
        rec_season = self.__parse_season(base["seasons"])
        total_seasons = mediainfo.number_of_seasons
        if rec_season and total_seasons and rec_season > int(total_seasons):
            out.append(dict(
                base, group="reco", kind="season_overflow", level="error",
                reason="季号超出该剧总季数（强烈疑似误配）",
                detail=(f"整理记录是 {base['seasons']}，但 tmdbid={mediainfo.tmdb_id}"
                        f"（《{mediainfo.title}》）在 TMDB 上总共只有 {total_seasons} 季。"
                        f"这正是「长剧被刮成冷门同名剧」的典型特征。"),
            ))

        # ④ 热度 / 别名数量 —— 脏词条特征
        vote_count = mediainfo.vote_count
        if vote_count is not None and int(vote_count) < self._min_vote:
            cat = (base["category"] or "").strip()
            if cat in _LOW_VOTE_CATEGORIES:
                # 实测教训（2026-09-16）：在中式媒体库里「低热度 ≠ 脏词条」。
                # 《天赐的声音》vote_count=2、《绝世战魂》vote_count=3，两个都是 TMDB
                # 上的正确条目（季号、总季数、原产国全对）；反倒是 vote_count=3279 的
                # 《惩罚者》(2004) 才是那条错配。中文综艺/国漫/国产剧在 TMDB 上本来
                # 就没什么人投票，据此报警会把真信号彻底淹没。
                out.append(dict(
                    base, group="reco", kind="low_vote_ok", level="info",
                    reason=f"低热度但属于正常范围（vote_count={vote_count}）",
                    detail=(f"tmdbid={mediainfo.tmdb_id}（《{mediainfo.title}》）在 TMDB 上只有 "
                            f"{vote_count} 人评分。该条目归类为「{cat}」，此类中文内容在 TMDB "
                            f"票数天然偏低，低热度本身不足以判定误配 —— 仅作提示，不计入异常。"),
                ))
            else:
                out.append(dict(
                    base, group="reco", kind="dirty_entry", level="warn",
                    reason=f"TMDB 条目热度极低（vote_count={vote_count}）",
                    detail=(f"tmdbid={mediainfo.tmdb_id}（《{mediainfo.title}》）评分人数只有 "
                            f"{vote_count}，低于阈值 {self._min_vote}。"
                            f"低热度又同名的条目，是 MP 误配的高发源。"),
                ))
        if len(alias_norms) < self._min_alias:
            out.append(dict(
                base, group="reco", kind="thin_aliases", level="info",
                reason=f"TMDB 条目别名过少（{len(alias_norms)} 个）",
                detail=(f"tmdbid={mediainfo.tmdb_id} 只有 {len(alias_norms)} 个别名，"
                        f"冷门/新建条目特征，建议人工确认。"),
            ))

        # ⑤ 原产国与分类目录错位
        countries = mediainfo.origin_country or []
        rec_category = base["category"] or ""
        if countries and rec_category:
            expect = self.__region_of(countries)
            if expect and expect not in rec_category:
                out.append(dict(
                    base, group="reco", kind="category_mismatch", level="info",
                    reason="分类目录与条目原产国不符",
                    detail=(f"tmdbid={mediainfo.tmdb_id} 的原产国是 "
                            f"{'/'.join(str(c) for c in countries)}（约 {expect}），"
                            f"但文件归进了「{rec_category}」。"),
                ))

        return out

    # ==================================================================
    # 工具方法
    # ==================================================================
    def __collect_records(self, days: int, cap: int) -> List[Any]:
        oper = TransferHistoryOper()
        tz = pytz.timezone(settings.TZ)
        today = datetime.datetime.now(tz=tz).date()
        start = today - datetime.timedelta(days=max(days, 1) - 1)

        seen, records = set(), []
        day = today
        while day >= start:
            try:
                rows = oper.list_by_date(day.strftime("%Y-%m-%d")) or []
            except Exception as e:
                logger.warning(f"【硬链接与识别校验】查询 {day} 整理记录失败：{e}")
                rows = []
            for r in rows:
                rid = getattr(r, "id", None)
                if rid in seen:
                    continue
                dt = self.__parse_dt(getattr(r, "date", ""))
                if dt is not None and dt.date() < start:
                    continue
                if rid is not None:
                    seen.add(rid)
                records.append(r)
                if len(records) >= cap:
                    return records
            day -= datetime.timedelta(days=1)
        return records

    @staticmethod
    def __parse_dt(text: str) -> Optional[datetime.datetime]:
        if not text:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.datetime.strptime(str(text).strip(), fmt)
            except ValueError:
                continue
        return None

    @staticmethod
    def __parse_season(text: str) -> Optional[int]:
        if not text:
            return None
        m = re.search(r"[Ss](\d{1,3})", str(text))
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
        m = re.search(r"\d{1,3}", str(text))
        if m:
            try:
                return int(m.group(0))
            except ValueError:
                return None
        return None

    @staticmethod
    def __norm(text) -> str:
        if not text:
            return ""
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(text).lower())

    @staticmethod
    def __to_int(value, default: int) -> int:
        try:
            if value is None or value == "":
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def __root_of(path: str) -> str:
        parts = Path(path).parts
        return str(Path(parts[0]) / parts[1]) if len(parts) > 1 else str(parts[0])

    @classmethod
    def __is_mounted(cls, path: str) -> bool:
        """MP 容器只挂了部分卷，没挂的卷要跳过而不是误报丢失。"""
        parts = Path(path).parts
        if len(parts) <= 2:
            return True
        root = str(Path(parts[0]) / parts[1])
        if not re.fullmatch(r"/volume\d+", root):
            return True
        return Path(parts[0]).joinpath(parts[1]).exists()

    @staticmethod
    def __region_of(countries: List[str]) -> Optional[str]:
        codes = {str(c).upper() for c in countries if c}
        for region, members in _REGION_MAP.items():
            if codes & set(members):
                return region
        return None

    # ==================================================================
    # 通知
    # ==================================================================
    def __notify_result(self, result: dict):
        if not self._notify:
            return
        checked, total = result["checked"], result["issues"]
        notes = result.get("notes", 0)
        cleaned = result.get("cleaned", 0)
        relinked = result.get("relinked", 0)
        pruned = result.get("pruned", 0)
        extra = []
        if cleaned:
            extra.append(f"已清理无效 {cleaned} 条")
        if relinked:
            extra.append(f"已自动补链 {relinked} 条")
        if pruned:
            extra.append(f"已删除僵尸整理记录 {pruned} 条")
        extra_txt = ("；" + "、".join(extra)) if extra else ""
        if total == 0:
            if checked:
                tail = f"另有 {notes} 条提示（非异常，见插件详情页{extra_txt}）。" if notes else ""
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【硬链接与识别校验】巡检通过 ✅",
                    text=(f"判定最近 {result['range_days']} 天共 {checked} 条整理记录，"
                          f"未发现硬链接断裂或识别错配"
                          f"（被更新版本覆盖的旧记录 {result['superseded']} 条已自动忽略）。{tail}"),
                )
            return

        errors = [i for i in result["items"] if i["level"] == "error"]
        lines = [f"判定 {checked} 条，发现 **{total}** 项异常："
                 f"断链 {result['link_bad']} / 识别 {result['reco_bad']}"
                 f"（其中严重 {len(errors)} 项）",
                 f"已自动忽略被覆盖旧记录 {result['superseded']} 条"
                 + (f"；另有 {notes} 条非异常提示" if notes else "")
                 + extra_txt, ""]
        for it in result["items"][:15]:
            lines.append(
                f"- [{_LEVEL_TEXT.get(it['level'], it['level'])}] "
                f"#{it['id']} 《{it['title']}》"
                f"{(' (' + it['year'] + ')') if it['year'] else ''}"
                f"\n    {it['reason']}｜{it['detail']}"
            )
        if total > 15:
            lines.append(f"\n… 其余 {total - 15} 项见插件详情页")

        self.post_message(
            mtype=NotificationType.Plugin,
            title=f"【硬链接与识别校验】发现 {total} 项异常 ⚠️",
            text="\n".join(lines),
        )

    # ==================================================================
    # 配置页
    # ==================================================================
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(2, self.__switch("enable", "启用插件")),
                            self.__col(2, self.__switch("notify", "异常时通知")),
                            self.__col(2, self.__switch("onlyonce", "立即运行一次")),
                            self.__col(2, self.__switch("check_link", "校验硬链接")),
                            self.__col(2, self.__switch("check_recognize", "校验识别")),
                            self.__col(2, self.__switch("deep", "深度复核(TMDB)")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(3, self.__switch("auto_relink", "自动补链(独立副本→硬链接)")),
                            self.__col(2, self.__text("relink_max", "每轮补链上限", "50")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(3, self.__switch("auto_prune", "自动清理僵尸整理记录")),
                            self.__col(2, self.__text("prune_max", "每轮清理上限", "50")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(4, self.__cron("cron", "定时执行周期", "0 5 * * *")),
                            self.__col(2, self.__text("days", "判定回溯天数", "3")),
                            self.__col(2, self.__text("max_records", "单次读取上限", "3000")),
                            self.__col(2, self.__text("max_deep", "深度复核上限", "300")),
                            self.__col(2, self.__text("min_vote", "热度下限", "10")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(12, {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": ("校验分三层：① 硬链接完整性（源种子与媒体库是否仍同一 inode）；"
                                             "② 识别自洽性（重新解析源文件名，比对年份/季号，零外部请求）；"
                                             "③ 深度复核（走 MP 真实识别链，比对 tmdbid、热度、季数、原产国，"
                                             "会消耗 TMDB 请求，受「深度复核上限」约束）。"),
                                },
                            }),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(12, {
                                "component": "VAlert",
                                "props": {
                                    "type": "warning",
                                    "variant": "tonal",
                                    "text": ("误报抑制 ①：同一媒体库路径会被反复整理覆盖"
                                             "（同一集 1080p / 2160p 会 hardlink 到同一 dest），"
                                             "插件只判定每个 dest 的**最新**记录，"
                                             "被覆盖的旧记录自动忽略并单独计数。"),
                                },
                            }),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(12, {
                                "component": "VAlert",
                                "props": {
                                    "type": "warning",
                                    "variant": "tonal",
                                    "text": ("误报抑制 ②：清理插件（如「清理媒体文件 / RemoveLink」）在"
                                             "下载文件被删除后会连带清掉媒体库硬链接与刮削文件、"
                                             "并删除空目录，但它**不删整理记录**。"
                                             "本插件检测到「源与目标整块都不存在」时判定为"
                                             "「已清理」，只作提示、不计异常。"),
                                },
                            }),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(12, {
                                "component": "VAlert",
                                "props": {
                                    "type": "warning",
                                    "variant": "tonal",
                                    "text": ("注意：只有挂载进 MoviePilot 容器的卷才能做 inode 比对。"
                                             "未挂载卷下的记录会被标记为「未挂载」跳过，"
                                             "不会误报为丢失。"),
                                },
                            }),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(12, {
                                "component": "VAlert",
                                "props": {
                                    "type": "error",
                                    "variant": "tonal",
                                    "text": ("「自动补链」默认**关闭**，开启后只会处理一种情况："
                                             "媒体库文件是**独立副本**（与源内容完全一致、仅 inode 不同，"
                                             "白占一份空间），此时按 inode 重建硬链接、释放空间。"
                                             "护栏：仅当该副本 st_nlink=1（孤本，不会牵连其他文件）、"
                                             "源文件存在、同文件系统、**字节数完全一致**时才动手；"
                                             "先建临时链接校验 inode 再原子替换，失败自动回滚。"
                                             "「目标文件不存在」的记录**不会**被修正 —— "
                                             "源已删除时无物可链，重新下载只会被清理插件再次删掉。"),
                                },
                            }),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(12, {
                                "component": "VAlert",
                                "props": {
                                    "type": "error",
                                    "variant": "tonal",
                                    "text": ("「自动清理僵尸整理记录」默认**关闭**。开启后只会清理一种记录："
                                             "**它指向的下载文件与媒体库文件都已不存在**"
                                             "（清理插件删种删库时不会顺手删掉这类残留，"
                                             "攒久了整理记录页会越来越脏）。"
                                             "护栏：① 源文件与目标路径所在卷都必须已挂载进容器"
                                             "（未挂载一律判为「看不见」，绝不当作「不存在」）；"
                                             "② 被更新记录接管（旧版本/旧路径）的记录保留不删；"
                                             "③ 受「每轮清理上限」约束，不会一次性删库；"
                                             "④ 每删一条都会把整行快照写进插件数据 `prune_log`"
                                             "（保留最近 10 轮），可翻查、可人工恢复。"
                                             "**它只删数据库记录一行，不会删除磁盘上的任何文件** —— "
                                             "做种源文件、媒体库母本、刮削产物全部不受影响。"),
                                },
                            }),
                        ],
                    },
                ],
            }
        ], {
            "enable": False,
            "notify": True,
            "onlyonce": False,
            "cron": "0 5 * * *",
            "days": 3,
            "check_link": True,
            "check_recognize": True,
            "deep": True,
            "max_records": 3000,
            "max_deep": 300,
            "min_vote": 10,
            "auto_relink": False,
            "relink_max": 50,
            "auto_prune": False,
            "prune_max": 50,
        }

    @staticmethod
    def __col(md: int, content) -> dict:
        return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": [content]}

    @staticmethod
    def __switch(model: str, label: str) -> dict:
        return {"component": "VSwitch", "props": {"model": model, "label": label}}

    @staticmethod
    def __text(model: str, label: str, placeholder: str = "") -> dict:
        return {"component": "VTextField",
                "props": {"model": model, "label": label, "placeholder": placeholder}}

    @staticmethod
    def __cron(model: str, label: str, placeholder: str = "") -> dict:
        return {"component": "VCronField",
                "props": {"model": model, "label": label, "placeholder": placeholder}}

    def __update_config(self):
        self.update_config({
            "enable": self._enable,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "days": self._days,
            "check_link": self._check_link,
            "check_recognize": self._check_recognize,
            "deep": self._deep,
            "max_records": self._max_records,
            "max_deep": self._max_deep,
            "min_vote": self._min_vote,
            "auto_relink": self._auto_relink,
            "relink_max": self._relink_max,
            "auto_prune": self._auto_prune,
            "prune_max": self._prune_max,
        })

    # ==================================================================
    # 详情页
    # ==================================================================
    def get_page(self) -> Optional[List[dict]]:
        last = self.get_data("last")
        history = self.get_data("history") or []

        run_btn = {
            "component": "VBtn",
            "props": {"prepend-icon": "mdi-magnify-scan", "variant": "tonal",
                      "color": "primary"},
            "text": "立即巡检",
            "events": {
                "click": {
                    "api": f"plugin/{self.__class__.__name__}/run"
                           f"?apikey={settings.API_TOKEN}",
                    "method": "post",
                }
            },
        }

        if not last:
            return [
                {
                    "component": "div",
                    "props": {"class": "d-flex align-center"},
                    "content": [
                        {"component": "h2", "props": {"class": "page-title m-0"},
                         "text": "硬链接与识别校验"},
                        {"component": "VSpacer"},
                        run_btn,
                    ],
                },
                {
                    "component": "div",
                    "text": "暂无巡检结果，点右上角「立即巡检」或到配置页打开「立即运行一次」。",
                    "props": {"class": "text-center mt-6 text-medium-emphasis"},
                },
            ]

        items = []
        for it in (last.get("items") or []):
            items.append({
                "level": _LEVEL_TEXT.get(it.get("level"), it.get("level")),
                "id": it.get("id"),
                "title": it.get("title") or "-",
                "year": it.get("year") or "-",
                "category": it.get("category") or "-",
                "tmdbid": it.get("tmdbid") if it.get("tmdbid") is not None else "-",
                "reason": it.get("reason") or "",
                "detail": it.get("detail") or "",
            })

        notes = []
        for it in (last.get("note_items") or []):
            notes.append({
                "level": _LEVEL_TEXT.get(it.get("level"), it.get("level")),
                "id": it.get("id"),
                "title": it.get("title") or "-",
                "year": it.get("year") or "-",
                "category": it.get("category") or "-",
                "tmdbid": it.get("tmdbid") if it.get("tmdbid") is not None else "-",
                "reason": it.get("reason") or "",
                "detail": it.get("detail") or "",
            })

        summary = [
            self.__stat("判定记录", f"{last.get('checked', 0)} 条"),
            self.__stat("异常合计", f"{last.get('issues', 0)} 项"),
            self.__stat("硬链接异常", f"{last.get('link_bad', 0)} 项"),
            self.__stat("识别异常", f"{last.get('reco_bad', 0)} 项"),
            self.__stat("提示(非异常)", f"{last.get('notes', 0)} 条", "grey"),
            self.__stat("已清理(无效)", f"{last.get('cleaned', 0)} 条", "grey"),
            self.__stat("已补链(释放空间)", f"{last.get('relinked', 0)} 条", "green"),
            self.__stat("已删僵尸记录", f"{last.get('pruned', 0)} 条", "green"),
            self.__stat("已忽略(被覆盖)", f"{last.get('superseded', 0)} 条", "grey"),
        ]

        hist_items = [
            {
                "time": h.get("time", ""),
                "range": f"最近 {h.get('range_days', '-')} 天",
                "checked": h.get("checked", 0),
                "issues": h.get("issues", 0),
                "link_bad": h.get("link_bad", 0),
                "reco_bad": h.get("reco_bad", 0),
                "notes": h.get("notes", 0),
                "superseded": h.get("superseded", 0),
                "pruned": h.get("pruned", 0),
            }
            for h in history
        ]

        page = [
            {
                "component": "div",
                "props": {"class": "d-flex align-center mb-2"},
                "content": [
                    {"component": "h2", "props": {"class": "page-title m-0"},
                     "text": "硬链接与识别校验"},
                    {"component": "VSpacer"},
                    {"component": "span",
                     "props": {"class": "text-medium-emphasis text-body-2 mr-3"},
                     "text": f"最近巡检：{last.get('time', '-')}"},
                    run_btn,
                ],
            },
            {"component": "VRow", "content": summary},
        ]

        if items:
            page.append({
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VDataTableVirtual",
                                "props": {
                                    "class": "text-sm",
                                    "headers": [
                                        {"title": "级别", "key": "level", "sortable": True, "width": "70"},
                                        {"title": "记录", "key": "id", "sortable": True, "width": "80"},
                                        {"title": "整理标题", "key": "title", "sortable": False},
                                        {"title": "年份", "key": "year", "sortable": False, "width": "70"},
                                        {"title": "分类", "key": "category", "sortable": False, "width": "90"},
                                        {"title": "TMDB", "key": "tmdbid", "sortable": False, "width": "90"},
                                        {"title": "判定", "key": "reason", "sortable": False},
                                        {"title": "说明", "key": "detail", "sortable": False},
                                    ],
                                    "items": items,
                                    "height": "34rem",
                                    "density": "compact",
                                    "fixed-header": True,
                                    "hover": True,
                                },
                            }
                        ],
                    }
                ],
            })
        else:
            page.append({
                "component": "div",
                "text": "本次巡检未发现异常 ✅",
                "props": {"class": "text-center mt-6 text-medium-emphasis"},
            })

        # 非异常提示（折叠展示，不触发通知）
        if notes:
            page.append({
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VExpansionPanels",
                                "props": {"variant": "accordion", "class": "mt-4"},
                                "content": [
                                    {
                                        "component": "VExpansionPanel",
                                        "content": [
                                            {
                                                "component": "VExpansionPanelTitle",
                                                "text": f"非异常提示（{last.get('notes', 0)} 条）"
                                                        f"｜已清理 {last.get('cleaned', 0)} · "
                                                        f"已补链 {last.get('relinked', 0)} · "
                                                        f"其余为命名风险等弱信号，仅供参考",
                                            },
                                            {
                                                "component": "VExpansionPanelText",
                                                "content": [
                                                    {
                                                        "component": "VDataTableVirtual",
                                                        "props": {
                                                            "class": "text-sm",
                                                            "headers": [
                                                                {"title": "记录", "key": "id", "width": "80"},
                                                                {"title": "整理标题", "key": "title"},
                                                                {"title": "年份", "key": "year", "width": "70"},
                                                                {"title": "TMDB", "key": "tmdbid", "width": "90"},
                                                                {"title": "判定", "key": "reason"},
                                                                {"title": "说明", "key": "detail"},
                                                            ],
                                                            "items": notes,
                                                            "height": "24rem",
                                                            "density": "compact",
                                                            "fixed-header": True,
                                                            "hover": True,
                                                        },
                                                    }
                                                ],
                                            },
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            })

        if hist_items:
            page.append({
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {"component": "h3",
                             "props": {"class": "text-h6 mt-4 mb-2"},
                             "text": "巡检历史"},
                            {
                                "component": "VDataTableVirtual",
                                "props": {
                                    "class": "text-sm",
                                    "headers": [
                                        {"title": "时间", "key": "time", "sortable": False},
                                        {"title": "范围", "key": "range", "sortable": False},
                                        {"title": "判定", "key": "checked", "sortable": True},
                                        {"title": "异常", "key": "issues", "sortable": True},
                                        {"title": "断链", "key": "link_bad", "sortable": True},
                                        {"title": "识别", "key": "reco_bad", "sortable": True},
                                        {"title": "提示", "key": "notes", "sortable": True},
                                        {"title": "已删记录", "key": "pruned", "sortable": True},
                                        {"title": "已忽略", "key": "superseded", "sortable": True},
                                    ],
                                    "items": hist_items,
                                    "height": "16rem",
                                    "density": "compact",
                                    "fixed-header": True,
                                    "hover": True,
                                },
                            },
                        ],
                    }
                ],
            })

        return page

    @staticmethod
    def __stat(title: str, value: str, color: str = None) -> dict:
        card_props = {"variant": "tonal", "class": "text-center py-2"}
        if color:
            card_props["color"] = color
        return {
            "component": "VCol",
            "props": {"cols": 6, "md": 3},
            "content": [
                {
                    "component": "VCard",
                    "props": card_props,
                    "content": [
                        {"component": "div",
                         "props": {"class": "text-caption text-medium-emphasis"},
                         "text": title},
                        {"component": "div",
                         "props": {"class": "text-h6"},
                         "text": str(value)},
                    ],
                }
            ],
        }
