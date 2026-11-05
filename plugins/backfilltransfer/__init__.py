# -*- coding: utf-8 -*-
"""
BackfillTransfer —— MoviePilot 存量补整理插件

== 解决什么问题 ==

MoviePilot 的整理有两条路，但两条都**不看存量**：

  1. 目录监控（`monitor_type = monitor`）
     inotify 事件 + 快照差分，只在**文件发生变化的那一刻**触发整理。
     文件落盘时没被捕获（容器重启、事件丢失、目录当时还没接进 MP），
     此后文件不再变化 → 永远不会有第二次机会。scheduler 里也没有任何
     周期性 `force_full_scan` 来兜底。

  2. 下载文件整理定时任务（`TransferChain.process`）
     开局就有一道硬闸门：

         if not any(d.monitor_type == "downloader" and d.storage == "local" ...):
             return True          # 未配置下载器监控目录，跳过整理

     当所有 Directory 都被配成「目录监控」时，这个任务**每轮空转**，
     一个种子都不处理。

结果就是：**`monitor_type=monitor` + 中途接管目录 = 那批存量文件永久缺席媒体库。**
本机实测 596 个文件（384 GB / 16 个资源包）就是这么漏掉的，其 mtime
全部早于对应目录的第一条整理记录。

== 本插件做什么 ==

扫描下载目录，把「**里面有视频、但整理记录里没有它们**」的资源包找出来，
交回 `TransferChain().do_transfer()` 补整理 —— 等价于你在「手动整理」里
一个个点，但可以一次扫全、可以试运行、可以定时。

判定「已整理」只用 MoviePilot 自己的 `transferhistory`：
  - 记录存在且 `status=True`  → 已整理，跳过
  - 记录存在但 `status=False` → **算未整理**，交给 MP 自己清理旧记录后重试
  - 没有记录                  → 未整理

== 为什么整包提交是安全的 ==

`do_transfer` 对目录会递归展开成一个个文件，然后**逐文件查整理历史**：

```python
if (not force or reorganize) and not preview:
    transferd = self._get_manual_transfer_history(fileitem=file_item, ...)
    if transferd:
        should_reorganize = manual and (reorganize or not transferd.status)
        ...
        skipped_history_count += 1
        continue          # 已成功的集直接跳过
```

所以一个包里有 30 集、其中 20 集早就整理过，提交整包只会处理剩下 10 集，
不会重复整理、不会重复刮削。传入 `manual=True` 时，**失败记录**会被
MP 先删掉再重试（`should_reorganize` 判的就是 `not status`）。

== 四道安全闸门（默认全开） ==

  1. **试运行（dry_run，默认开）**
     走 `do_transfer(preview=True)`，只算规划、不落盘、不改库。
     第一次跑请先看试运行结果，确认规划路径对得上再关掉它。

  2. **只在种子已完成时整理（only_completed，默认开）**
     把包名与下载器里的种子比对；命中且 `progress < 100%` 的包直接跳过。
     这是「边下边整理」的根因防护 —— 半成品文件被整理进库会变成
     `*!qB*` 之类的垃圾条目，且原种校验永远到不了 100%。

  3. **冷却时间（cooldown_min，默认 10 分钟）**
     包内最新 mtime 距今不足 N 分钟的跳过。tr/qb 写盘期间 mtime 在跳，
     这条是第 2 道的补充（种子已下完但还有后处理/校验时也会命中）。

  4. **体积下限 / 屏蔽词 / 回收站与临时文件过滤**
     `.part` / `.!qB` / `@Recycle` / `#recycle` / `@eaDir` / 隐藏目录一律不碰。

  5. **已在库识别（inode 比对，默认开）**
     有一类「假候选」：文件其实**早就进过库**，只是 transferhistory 记录被
     清理插件（RemoveLink 配 `delete_history=true`）连记录一起删了 ——
     看起来「没有整理记录」，但 `st_nlink=2`，另一条链接就落在 `Media/已整理` 下。
     再整理一次会在库里产生**第二个条目**（同一内容两份条目、双份刮削）。
     所以扫描后会拿候选文件的 `(dev, ino)` 去比媒体库的 inode 集合，
     命中即跳过，并在详情页单列「已在库（缺记录）」。
     实测本机 23 个候选里有 7 个属于这一类（62 GB），全部被这道闸门拦住。

== 铁律 ==

- 只提交整理，**不删任何文件**。整理模式沿用 Directory 配置（`link` = 硬链接）。
- 每轮包数受「单轮提交上限」约束，不会一次性把队列打爆。
- 扫描到的候选清单写进插件数据，页面可查；异常会通知。
- `scrape=True` 时整理完立刻刮削（`do_transfer` 的 `scrape` 参数），
  避免出现「入库成功但零刮削」的静默失败。

"""

import datetime
import os
import posixpath
import threading
import traceback
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.db import SessionFactory
from app.db.models.transferhistory import TransferHistory
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.directory import DirectoryHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import FileItem
from app.schemas.types import NotificationType

# 视频后缀（与 MoviePilot 的媒体文件判定保持一致的常用集合）
_VIDEO_EXT = {
    ".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mts", ".iso", ".wmv", ".mov",
    ".rmvb", ".rm", ".flv", ".m4v", ".mpg", ".mpeg", ".webm", ".vob", ".strm",
    ".divx", ".asf", ".f4v", ".3gp", ".ogm", ".xvid", ".m2v", ".mxf",
}

# 扫描时整体跳过的目录名（回收站 / 缩略图 / 临时 / 绿联隐藏目录）
_SKIP_DIRS = {
    "@recycle", "#recycle", "@eadir", ".@__thumb", "@tmp", "@tmpdir",
    "@thumbnail", "@search", "@video", "@video_web", "lost+found",
    "node_modules", "$recycle.bin", "system volume information",
}

# 文件级黑名单片段（未完成 / 临时 / 隐藏）
_SKIP_NAME_PARTS = (".!qb", ".part", ".!ut", ".aria2", ".downloading",
                    ".tmp", ".crdownload", ".bts", ".bc!")

_MAX_HISTORY = 10


class BackfillTransfer(_PluginBase):
    # ---- 插件元信息 ----
    plugin_name = "存量补整理"
    plugin_desc = ("把「躺在下载目录里、却没有整理记录」的存量资源补进媒体库。"
                   "MoviePilot 的整理只认文件变化事件（目录监控）或下载器完成事件"
                   "（要求 Directory 配成「下载器监控」），当目录被配成「目录监控」时，"
                   "定时任务「下载文件整理」会在开局直接 return —— 于是"
                   "「中途接管目录 / 容器重启丢事件」的那批文件永久缺席。"
                   "本插件扫描下载目录，用 transferhistory 判定哪些资源包还没整理，"
                   "再交回 MoviePilot 自己的整理链补做（整包提交，已整理的集由 MP 自行跳过，"
                   "失败记录由 MP 清理后重试）。四道安全闸门：试运行预览、"
                   "只在种子 100% 完成后整理、写盘冷却时间、体积下限与屏蔽词。"
                   "只整理、不删文件，整理方式沿用目录配置（硬链接）。")
    plugin_version = "1.0.4"
    plugin_author = "spizmm"
    plugin_icon = "https://raw.githubusercontent.com/yuez414-eng/MoviePilot-Plugins/main/icons/backfilltransfer.png"
    author_url = "https://github.com/yuez414-eng"
    plugin_config_prefix = "BackfillTransfer_"
    plugin_order = 51
    auth_level = 1

    # ---- 运行期状态 ----
    _enable: bool = False
    _notify: bool = True
    _onlyonce: bool = False
    _cron: str = "0 4 * * *"
    _dirs: str = ""
    _dry_run: bool = True
    _only_completed: bool = True
    _scrape: bool = True
    _min_size_mb: int = 0
    _cooldown_min: int = 10
    _max_pkgs: int = 30
    _max_files: int = 200000
    _exclude_words: str = ""
    _include_risky: bool = False
    _skip_conflict: bool = True

    _running: bool = False
    _progress: str = ""
    _scheduler: Optional[BackgroundScheduler] = None
    _lock = threading.Lock()

    # ==================================================================
    # 生命周期
    # ==================================================================
    def init_plugin(self, config: dict = None):
        self.stop_service()
        config = config or {}
        self._enable = bool(config.get("enable", False))
        self._notify = bool(config.get("notify", True))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._cron = (config.get("cron") or "0 4 * * *").strip()
        self._dirs = (config.get("dirs") or "").strip()
        self._dry_run = bool(config.get("dry_run", True))
        self._only_completed = bool(config.get("only_completed", True))
        self._scrape = bool(config.get("scrape", True))
        self._min_size_mb = self.__to_int(config.get("min_size_mb"), 0)
        self._cooldown_min = self.__to_int(config.get("cooldown_min"), 10)
        self._max_pkgs = max(1, self.__to_int(config.get("max_pkgs"), 30))
        self._max_files = max(1000, self.__to_int(config.get("max_files"), 200000))
        self._exclude_words = (config.get("exclude_words") or "").strip()
        self._include_risky = bool(config.get("include_risky", False))
        self._skip_conflict = bool(config.get("skip_conflict", True))

        if self._onlyonce:
            logger.info("【存量补整理】立即运行一次")
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=lambda: self.__start(mode="auto"),
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
                "summary": "扫描或补整理",
                "description": "mode=scan 只扫描；mode=apply 按当前配置执行补整理（受试运行开关约束）",
            },
            {
                "path": "/one",
                "endpoint": self.__api_one,
                "methods": ["POST"],
                "summary": "单个资源包整理",
                "description": "path=资源包绝对路径，apply=false 只试运行；apply=true 真正整理",
            },
            {
                "path": "/status",
                "endpoint": self.__api_status,
                "methods": ["GET"],
                "summary": "查询运行状态",
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enable and self._cron:
            try:
                trigger = CronTrigger.from_crontab(self._cron)
            except Exception as e:
                logger.error(f"【存量补整理】cron 表达式无效：{self._cron} - {e}")
                return []
            return [
                {
                    "id": "BackfillTransfer",
                    "name": "存量补整理",
                    "trigger": trigger,
                    "func": lambda: self.__start(mode="auto"),
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
            logger.error(f"【存量补整理】停止服务失败：{e}")

    # ==================================================================
    # API
    # ==================================================================
    def __api_run(self, mode: str = "scan") -> dict:
        mode = (mode or "scan").lower()
        if mode not in ("scan", "apply", "auto"):
            return {"success": False, "message": f"不支持的 mode：{mode}"}
        ok = self.__start(mode=mode)
        if not ok:
            return {"success": False, "message": "已有任务在运行，请稍候"}
        return {"success": True,
                "message": "已开始扫描" if mode == "scan" else "已开始补整理（后台执行）"}

    def __api_one(self, path: str = "", apply: bool = False) -> dict:
        """针对单个资源包执行（dry-run / 真整理）。

        用于「只想补其中几个包」的场景 —— 全量 apply 会把识别错的、目标冲突的
        一起塞进整理队列，而失败的整理会留下 status=false 的记录，
        反过来让目录监控永久跳过这批文件（dispatcher 见到记录就 return False）。
        """
        path = (path or "").strip().replace("\\", "/").rstrip("/")
        if not path or not os.path.exists(path):
            return {"success": False, "message": f"路径不存在：{path}"}
        pkg = self.__pkg_of(path)
        if not pkg:
            return {"success": False, "message": "该路径下没有视频文件，或不在已配置的下载目录内"}
        if not apply:
            msg = self.__preview_one(pkg)
            return {"success": True, "message": "试运行完成",
                    "data": {"msg": msg, "titles": pkg.get("titles") or [],
                             "conflict": pkg.get("conflict", 0),
                             "plan_sample": pkg.get("plan_sample", "")}}
        state, msg = self.__apply_one(pkg)
        return {"success": state == "ok", "message": msg,
                "data": {"state": state, "name": pkg["name"]}}

    def __pkg_of(self, path: str) -> Optional[dict]:
        """把任意下载目录内的路径包成 pkg 结构（用于单包操作）"""
        root = None
        for r in self.__roots():
            if path.startswith(r["path"] + "/") or path == r["path"]:
                root = r
                break
        if root is None:
            return None
        entry = None
        rel = path[len(root["path"]):].lstrip("/")
        first = rel.split("/")[0]
        try:
            entry = next((e for e in os.scandir(root["path"]) if e.name == first), None)
        except Exception:
            return None
        if entry is None:
            return None
        stat = {"files": 0, "pkgs": 0}
        videos, total, _newest = self.__collect(entry, 0, stat)
        if not videos:
            return None
        return {
            "name": entry.name,
            "path": (entry.path or "").replace("\\", "/"),
            "root": root["path"],
            "storage": root["storage"],
            "is_dir": bool(entry.is_dir()),
            "files": len(videos),
            "total_files": total,
            "gb": round(sum(v[1] for v in videos) / 1024 ** 3, 2),
            "progress": None,
            "state": "pending",
            "msg": "",
        }

    def __api_status(self) -> dict:
        return {"running": self._running, "progress": self._progress,
                "last": self.get_data("last") or {}}

    def __start(self, mode: str = "auto") -> bool:
        if not self._lock.acquire(blocking=False):
            logger.warning("【存量补整理】上一次任务尚未结束，跳过本次触发")
            return False
        try:
            if self._running:
                return False
            self._running = True
        finally:
            self._lock.release()

        def _worker():
            try:
                self.__task(mode=mode)
            except Exception as e:
                logger.error(f"【存量补整理】任务异常：{e} - {traceback.format_exc()}")
            finally:
                self._running = False
                self._progress = ""

        threading.Thread(target=_worker, name="BackfillTransfer", daemon=True).start()
        return True

    # ==================================================================
    # 主流程
    # ==================================================================
    def __task(self, mode: str = "auto") -> dict:
        started = datetime.datetime.now(tz=pytz.timezone(settings.TZ))
        # mode=scan 强制试运行；mode=apply 遵守配置；auto 遵守配置（定时场景）
        dry_run = True if mode == "scan" else self._dry_run

        self._progress = "正在读取整理记录 ..."
        logger.info(f"【存量补整理】开始（{'试运行' if dry_run else '实际整理'}），"
                    f"mode={mode}")

        try:
            organized = self.__organized_index()
        except Exception as e:
            logger.error(f"【存量补整理】读取整理记录失败：{e}")
            return {"error": str(e)}

        self._progress = "正在建立种子索引 ..."
        torrents = self.__torrent_index() if self._only_completed else {}

        self._progress = "正在索引媒体库 inode（防重复入坑）..."
        lib_inodes = set() if self._include_risky else self.__library_inodes()

        self._progress = "正在扫描下载目录 ..."
        candidates, excluded, scanned = self.__scan(
            organized, torrents, lib_inodes, check_risky=not self._include_risky)

        result = {
            "time": started.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": mode,
            "dry_run": dry_run,
            "roots": len(self.__roots()),
            "scanned_files": scanned["files"],
            "scanned_pkgs": scanned["pkgs"],
            "candidates": len(candidates),
            "files": sum(c["files"] for c in candidates),
            "gb": round(sum(c["gb"] for c in candidates), 2),
            "in_library": sum(1 for c in excluded if c["state"] == "in_library"),
            "in_library_files": sum(c["files"] for c in excluded
                                    if c["state"] == "in_library"),
            "shared": sum(1 for c in excluded if c["state"] == "shared"),
            "shared_files": sum(c["files"] for c in excluded
                                if c["state"] == "shared"),
            "submitted": 0,
            "ok": 0,
            "fail": 0,
            "skip": 0,
            "items": [dict(c, msg=self.__exclude_reason(c)) for c in excluded],
        }

        if not candidates:
            self._progress = ""
            logger.info("【存量补整理】没有发现需要补整理的资源包")
            self.__save_result(result)
            return result

        # ---- 执行 ----
        todo = candidates[:self._max_pkgs]
        skipped_by_limit = len(candidates) - len(todo)
        for idx, pkg in enumerate(todo, start=1):
            self._progress = (f"正在整理（{idx}/{len(todo)}）{pkg['name']} "
                              f"[{pkg['files']} 个文件 / {pkg['gb']:.1f} GB]")
            logger.info(f"【存量补整理】{self._progress}")
            item = dict(pkg)
            if dry_run:
                item["state"] = "preview"
                item["msg"] = self.__preview_one(item)
            else:
                state, msg = self.__apply_one(pkg)
                item["state"] = state
                item["msg"] = msg
                result["submitted"] += 1
                if state == "ok":
                    result["ok"] += 1
                elif state == "fail":
                    result["fail"] += 1
                elif state == "skip":
                    result["skip"] += 1
            result["items"].append(item)

        if skipped_by_limit > 0:
            result["skipped_by_limit"] = skipped_by_limit
            logger.info(f"【存量补整理】另有 {skipped_by_limit} 个包超出单轮上限，"
                        f"下轮继续")

        self._progress = ""
        self.__save_result(result)
        self.__notify_result(result)
        logger.info(f"【存量补整理】结束：候选 {result['candidates']} 包 / "
                    f"{result['files']} 文件 / {result['gb']} GB，"
                    f"提交 {result['submitted']}，成功 {result['ok']}，"
                    f"失败 {result['fail']}，跳过 {result.get('skip', 0)}")
        return result

    # ==================================================================
    # 扫描
    # ==================================================================
    def __roots(self) -> List[Dict[str, str]]:
        """待扫描的下载目录。配置里有 dirs 就用配置，否则自动取 MP 的下载目录。"""
        roots: List[Dict[str, str]] = []
        if self._dirs:
            for line in self._dirs.replace("；", "\n").replace(";", "\n").replace(
                    ",", "\n").splitlines():
                p = line.strip()
                if not p:
                    continue
                p = p.replace("\\", "/").rstrip("/")
                if os.path.isdir(p):
                    roots.append({"path": p, "storage": "local"})
                else:
                    logger.warning(f"【存量补整理】配置的目录不存在，已忽略：{p}")
            return roots

        try:
            for d in DirectoryHelper().get_download_dirs():
                if d.storage != "local" or not d.download_path:
                    continue
                p = str(d.download_path).replace("\\", "/").rstrip("/")
                if os.path.isdir(p) and p not in [r["path"] for r in roots]:
                    roots.append({"path": p, "storage": d.storage})
        except Exception as e:
            logger.error(f"【存量补整理】读取下载目录失败：{e}")
        return roots

    def __organized_index(self) -> Dict[str, bool]:
        """src 路径 -> 是否整理成功（同一 src 取 id 最大的记录）"""
        latest: Dict[str, bool] = {}
        with SessionFactory() as session:
            rows = (session.query(TransferHistory.src, TransferHistory.status,
                                  TransferHistory.id)
                    .order_by(TransferHistory.id)
                    .all())
        for src, status, _id in rows:
            if not src:
                continue
            latest[self.__norm(str(src))] = bool(status)
        logger.info(f"【存量补整理】整理记录 {len(latest)} 条（唯一源路径）")
        return latest

    def __torrent_index(self) -> Dict[str, float]:
        """包名 -> 最高进度（用于「只在种子已完成时整理」这道闸门）"""
        idx: Dict[str, float] = {}
        try:
            from app.helper.downloader import DownloaderHelper
            helper = DownloaderHelper()
            names = [c.get("name") for c in (SystemConfigOper().get("Downloaders") or [])]
            for name in names:
                if not name:
                    continue
                try:
                    service = helper.get_service(name)
                    client = getattr(service, "instance", None)
                    if client is None:
                        continue
                    torrents, has_err = client.get_torrents()
                    if has_err or not torrents:
                        continue
                    for t in torrents:
                        tname = getattr(t, "name", None)
                        if not tname:
                            continue
                        prog = self.__norm_progress(getattr(t, "progress", None))
                        if prog is None:
                            continue
                        key = self.__norm(str(tname))
                        idx[key] = max(idx.get(key, 0.0), prog)
                except Exception as e:
                    logger.warning(f"【存量补整理】读取下载器 {name} 种子列表失败：{e}")
        except Exception as e:
            logger.warning(f"【存量补整理】建立种子索引失败，跳过完成度校验：{e}")
            return {}
        logger.info(f"【存量补整理】下载器种子索引 {len(idx)} 条")
        return idx

    def __library_inodes(self) -> set:
        """媒体库里所有视频文件的 (dev, ino) 集合。

        用途：某些文件其实**早就进过库**，只是 transferhistory 记录被清理插件
        （RemoveLink 配 delete_history=true）一并删掉了。它们看起来「没有整理记录」，
        但 nlink=2 且另一条链接就落在媒体库里 —— 这时再整理一次就会在库里
        产生第二个条目。所以先建库内 inode 集合，命中即跳过。
        """
        keys = set()
        roots = set()
        try:
            for d in DirectoryHelper().get_dirs():
                if d.storage != "local" or not d.library_path:
                    continue
                roots.add(str(d.library_path).replace("\\", "/").rstrip("/"))
        except Exception as e:
            logger.warning(f"【存量补整理】读取媒体库目录失败：{e}")
            return keys

        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames
                               if d.lower() not in _SKIP_DIRS and not d.startswith(".")]
                for fn in filenames:
                    if os.path.splitext(fn.lower())[1] not in _VIDEO_EXT:
                        continue
                    try:
                        st = os.stat(os.path.join(dirpath, fn))
                    except Exception:
                        continue
                    keys.add((st.st_dev, st.st_ino))
        logger.info(f"【存量补整理】媒体库视频 inode {len(keys)} 个（{len(roots)} 个库根）")
        return keys

    def __scan(self, organized: Dict[str, bool], torrents: Dict[str, float],
               lib_inodes: set, check_risky: bool = True
               ) -> Tuple[List[dict], List[dict], Dict[str, int]]:
        now = datetime.datetime.now().timestamp()
        cooldown = max(0, self._cooldown_min) * 60
        min_size = max(0, self._min_size_mb) * 1024 * 1024
        excludes = [w.strip().lower() for w in
                    self._exclude_words.replace("；", ",").replace(";", ",").split(",")
                    if w.strip()]

        candidates: List[dict] = []
        excluded: List[dict] = []
        stat = {"files": 0, "pkgs": 0}

        for root in self.__roots():
            base = root["path"]
            try:
                entries = sorted(os.scandir(base), key=lambda e: e.name)
            except Exception as e:
                logger.warning(f"【存量补整理】无法读取目录 {base}：{e}")
                continue

            for entry in entries:
                if stat["files"] >= self._max_files:
                    logger.warning("【存量补整理】已达单次扫描文件上限，停止扫描")
                    break
                name_low = entry.name.lower()
                if entry.is_dir() and name_low in _SKIP_DIRS:
                    continue
                if name_low.startswith("."):
                    continue
                if excludes and any(w in name_low for w in excludes):
                    continue

                videos, total_files, newest = self.__collect(entry, min_size, stat)
                if not videos:
                    continue
                stat["pkgs"] += 1

                missing = [v for v in videos if not organized.get(self.__norm(v[0]))]
                if not missing:
                    continue

                # 闸门：冷却时间（还在写盘）
                if cooldown and newest and (now - newest) < cooldown:
                    logger.debug(f"【存量补整理】{entry.name} 最近 "
                                 f"{int((now - newest) / 60)} 分钟内有写入，跳过")
                    continue

                # 闸门：种子未完成
                prog = torrents.get(self.__norm(entry.name))
                if self._only_completed and prog is not None and prog < 0.999:
                    logger.info(f"【存量补整理】{entry.name} 种子进度 "
                                f"{prog * 100:.1f}%，跳过（防边下边整理）")
                    continue

                size = sum(v[1] for v in missing)
                pkg = {
                    "name": entry.name,
                    "path": (entry.path or "").replace("\\", "/"),
                    "root": base,
                    "storage": root["storage"],
                    "is_dir": bool(entry.is_dir()),
                    "files": len(missing),
                    "total_files": len(videos),
                    "gb": round(size / 1024 ** 3, 2),
                    "progress": round(prog * 100, 1) if prog is not None else None,
                    "state": "pending",
                    "msg": "",
                }

                # 闸门：两类「假候选」，默认都排除
                if check_risky:
                    # ① 文件其实已在媒体库（记录被清理插件连记录一起删了）
                    hit = 0
                    if lib_inodes:
                        hit = sum(1 for v in missing if (v[2], v[3]) in lib_inodes)
                    if hit:
                        pkg["in_lib"] = hit
                        pkg["state"] = "in_library"
                        excluded.append(pkg)
                        logger.info(f"【存量补整理】{entry.name} 有 {hit}/{len(missing)} "
                                    f"个文件已在媒体库，跳过（避免重复入库）")
                        continue
                    # ② 还有别的硬链接 —— 同片另一版本通常已在库
                    linked = sum(1 for v in missing if len(v) > 4 and v[4] > 1)
                    if linked:
                        pkg["in_lib"] = 0
                        pkg["shared"] = linked
                        pkg["state"] = "shared"
                        excluded.append(pkg)
                        logger.info(f"【存量补整理】{entry.name} 有 {linked}/{len(missing)} "
                                    f"个文件存在其它硬链接，跳过（同片另一版本可能已在库）")
                        continue

                candidates.append(pkg)

        candidates.sort(key=lambda c: -c["gb"])
        excluded.sort(key=lambda c: -c["gb"])
        return candidates, excluded, stat

    def __collect(self, entry, min_size: int,
                  stat: Dict[str, int]
                  ) -> Tuple[List[Tuple[str, int, int, int, int]], int, float]:
        """递归收集一个资源包里的视频 -> [(path, size, dev, ino, nlink)], 文件总数, 最新 mtime"""
        videos: List[Tuple[str, int, int, int, int]] = []
        total = 0
        newest = 0.0

        def _push(path: str, st):
            nonlocal newest
            if st.st_mtime > newest:
                newest = st.st_mtime
            videos.append((path, st.st_size, st.st_dev, st.st_ino, st.st_nlink))

        try:
            if entry.is_file(follow_symlinks=False):
                low = entry.name.lower()
                if any(p in low for p in _SKIP_NAME_PARTS):
                    return [], 0, 0.0
                if os.path.splitext(low)[1] not in _VIDEO_EXT:
                    return [], 0, 0.0
                st = entry.stat()
                stat["files"] += 1
                if st.st_size < min_size:
                    return [], 1, st.st_mtime
                _push((entry.path or "").replace("\\", "/"), st)
                return videos, 1, newest

            for dirpath, dirnames, filenames in os.walk(entry.path):
                dirnames[:] = [d for d in dirnames
                               if d.lower() not in _SKIP_DIRS and not d.startswith(".")]
                for fn in filenames:
                    if stat["files"] >= self._max_files:
                        return videos, total, newest
                    low = fn.lower()
                    if any(p in low for p in _SKIP_NAME_PARTS):
                        continue
                    if os.path.splitext(low)[1] not in _VIDEO_EXT:
                        continue
                    full = os.path.join(dirpath, fn).replace("\\", "/")
                    try:
                        st = os.stat(full)
                    except Exception:
                        continue
                    total += 1
                    stat["files"] += 1
                    if st.st_size < min_size:
                        continue
                    _push(full, st)
        except Exception as e:
            logger.debug(f"【存量补整理】收集 {entry.path} 出错：{e}")
        return videos, total, newest

    # ==================================================================
    # 执行整理
    # ==================================================================
    def __fileitem(self, pkg: dict) -> Optional[FileItem]:
        path = pkg["path"]
        if pkg["is_dir"]:
            return FileItem(storage=pkg["storage"], path=path, type="dir",
                            name=pkg["name"])
        base = posixpath.basename(path)
        stem, ext = posixpath.splitext(base)
        try:
            size = os.stat(path).st_size
        except Exception:
            size = 0
        return FileItem(storage=pkg["storage"], path=path, type="file", name=base,
                        basename=stem, extension=ext.lstrip("."), size=size)

    def __plan_one(self, pkg: dict) -> Tuple[bool, Any]:
        """只做规划（不改文件），并记录识别标题 / 目标路径 / 目标冲突数。

        目标冲突 = 规划出的目标文件在库里**已经存在同名文件**。
        这种情况几乎都是「库里早就有一版，下载目录里躺着另一版」，
        直接整理会覆盖已有集数（或产生重复条目），必须先看见再决定。
        """
        from app.chain.transfer import TransferChain
        fileitem = self.__fileitem(pkg)
        if fileitem is None:
            return False, "文件项构建失败"
        try:
            ok, ret = TransferChain().do_transfer(
                fileitem=fileitem, manual=True, background=False,
                scrape=self._scrape, preview=True,
            )
        except Exception as e:
            logger.error(f"【存量补整理】试运行 {pkg['name']} 出错：{e}")
            return False, f"试运行异常：{e}"
        if not isinstance(ret, dict):
            pkg["conflict"] = 0
            return ok, ret

        items = ret.get("items") or []
        titles: List[str] = []
        dirs: List[str] = []
        conflicts = 0
        for it in items:
            t = (it.get("title") or "").strip()
            if t and t not in titles:
                titles.append(t)
            td = (it.get("target_dir") or "").strip()
            if td and td not in dirs:
                dirs.append(td)
            tgt = (it.get("target") or "").strip()
            if tgt and os.path.exists(tgt):
                conflicts += 1
        pkg["conflict"] = conflicts
        pkg["titles"] = titles[:6]
        pkg["plan_dirs"] = [d.rsplit("/", 1)[-1] for d in dirs[:6]]
        pkg["plan_total"] = (ret.get("summary") or {}).get("total", 0)
        if items:
            pkg["plan_sample"] = (items[0].get("target") or "")
        return ok, ret

    def __preview_one(self, pkg: dict) -> str:
        """试运行：只算规划，同时把识别出的标题与目标路径样本记下来。

        这一步的价值不只是「能不能整理」，更是**识别对不对** ——
        历史上出现过《灰欲人生》被识别成《人间余味 (2021)》这类误配，
        只看「成功 62/62」是看不出来的，必须把目标路径摊开。
        """
        ok, ret = self.__plan_one(pkg)
        if not isinstance(ret, dict):
            if isinstance(ret, str) and (ret.startswith("文件项") or
                                         ret.startswith("试运行异常")):
                return ret
            return "规划完成" if ok else f"试运行不可行：{str(ret)[:120]}"

        summary0 = ret.get("summary") or {}
        items0 = ret.get("items") or []
        if not items0 and not ok:
            return f"试运行不可行：{str(ret.get('message') or ret)[:150]}"

        titles = pkg.get("titles") or []
        conflicts = pkg.get("conflict") or 0

        fails = [it for it in items0 if not it.get("success")]
        reasons: List[str] = []
        for it in fails:
            r = (it.get("message") or "失败").strip()
            if r not in reasons:
                reasons.append(r)

        msg = (f"规划 {summary0.get('total', 0)} 个"
               f"（成功 {summary0.get('success', 0)}/失败 {summary0.get('failed', 0)}）")
        if titles:
            msg += "｜识别：" + "、".join(titles[:2])
            if len(titles) > 1:
                msg += f" 等 {len(titles)} 个标题"
        if conflicts:
            msg += f"｜⚠️ 目标已存在 {conflicts} 个（会覆盖库内已有）"
        if fails:
            msg = "⚠️ " + msg + "｜失败原因：" + "；".join(reasons[:2])
        return msg

    def __apply_one(self, pkg: dict) -> Tuple[str, str]:
        from app.chain.transfer import TransferChain
        fileitem = self.__fileitem(pkg)
        if fileitem is None:
            return "fail", "文件项构建失败"

        # 护栏：先规划一遍，若目标与库里已有文件重名 → 整包跳过。
        # 宁可少整理一包，也不能把已有的集数覆盖掉（覆盖会带走旧文件的硬链接）。
        if self._skip_conflict:
            try:
                ok0, ret0 = self.__plan_one(pkg)
            except Exception as e:
                logger.warning(f"【存量补整理】{pkg['name']} 规划预检失败：{e}")
                ok0, ret0 = True, {}
            conflict = pkg.get("conflict") or 0
            if conflict:
                total = pkg.get("plan_total") or conflict
                dirs = "、".join(pkg.get("plan_dirs") or [])
                return "skip", (f"⚠️ 目标已存在 {conflict}/{total} 个"
                                f"（{dirs}），已跳过 —— 库内已有同集数，"
                                f"整理会覆盖。确认要替换请关掉「目标冲突时跳过」")
            if not ok0 and isinstance(ret0, dict):
                summary0 = ret0.get("summary") or {}
                if summary0.get("success", 0) == 0:
                    return "fail", "规划全部失败（未识别到媒体信息），已跳过"

        try:
            ok, ret = TransferChain().do_transfer(
                fileitem=fileitem, manual=True, background=False,
                scrape=self._scrape, preview=False,
            )
        except Exception as e:
            logger.error(f"【存量补整理】整理 {pkg['name']} 异常：{e}")
            return "fail", f"{e}"
        if not ok:
            return "fail", str(ret)[:300]
        return "ok", str(ret)[:300] if ret else "完成"

    # ==================================================================
    # 结果持久化 / 通知
    # ==================================================================
    def __save_result(self, result: dict):
        # 明细太长时截断，避免插件数据过大
        result["items"] = result.get("items", [])[:300]
        self.save_data("last", result)
        history = self.get_data("history") or []
        history.insert(0, {
            "time": result.get("time", ""),
            "mode": result.get("mode", ""),
            "dry_run": result.get("dry_run", True),
            "candidates": result.get("candidates", 0),
            "files": result.get("files", 0),
            "gb": result.get("gb", 0),
            "submitted": result.get("submitted", 0),
            "ok": result.get("ok", 0),
            "fail": result.get("fail", 0),
            "skip": result.get("skip", 0),
        })
        self.save_data("history", history[:_MAX_HISTORY])

    def __notify_result(self, result: dict):
        if not self._notify or not result.get("candidates"):
            return
        title = ("【存量补整理】试运行完成 📋" if result.get("dry_run")
                 else f"【存量补整理】补整理完成：成功 {result.get('ok', 0)} / "
                      f"失败 {result.get('fail', 0)}")
        lines = [f"扫描 {result.get('roots', 0)} 个下载目录、"
                 f"{result.get('scanned_files', 0)} 个文件，"
                 f"发现 **{result.get('candidates', 0)}** 个待补整理资源包"
                 f"（{result.get('files', 0)} 个文件 / {result.get('gb', 0)} GB）"]
        if result.get("dry_run"):
            lines.append("本次为**试运行**，未改动任何文件。确认规划无误后到插件配置页关闭"
                         "「试运行」再执行。")
        if result.get("skipped_by_limit"):
            lines.append(f"另有 {result['skipped_by_limit']} 个包超出单轮上限，下轮继续。")
        lines.append("")
        for it in (result.get("items") or [])[:12]:
            state = {"preview": "试运行", "ok": "成功", "fail": "失败",
                     "skip": "跳过"}.get(it.get("state"), it.get("state"))
            lines.append(f"- [{state}] {it.get('name', '')[:52]} "
                         f"({it.get('files', 0)} 个 / {it.get('gb', 0)} GB) "
                         f"{it.get('msg', '')[:80]}")
        if len(result.get("items") or []) > 12:
            lines.append(f"… 其余 {len(result['items']) - 12} 项见插件详情页")
        try:
            self.post_message(mtype=NotificationType.Plugin, title=title,
                              text="\n".join(lines))
        except Exception as e:
            logger.error(f"【存量补整理】发送通知失败：{e}")

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
                            self.__col(2, self.__switch("notify", "完成后通知")),
                            self.__col(2, self.__switch("onlyonce", "立即运行一次")),
                            self.__col(2, self.__switch("dry_run", "试运行(不动文件)")),
                            self.__col(2, self.__switch("only_completed", "只在种子完成后整理")),
                            self.__col(2, self.__switch("scrape", "整理后刮削")),
                            self.__col(2, self.__switch("skip_conflict", "目标冲突时跳过")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(4, self.__switch("include_risky",
                                                        "强行整理已排除项（危险）")),
                            self.__col(4, self.__cron("cron", "定时执行周期", "0 4 * * *")),
                            self.__col(4, self.__text("exclude_words", "目录名屏蔽词（逗号分隔）",
                                                      "预告,花絮")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(4, self.__text("dirs", "扫描目录（留空=自动取下载目录）",
                                                      "/volume3/HDD_03/3.3t")),
                            self.__col(2, self.__text("min_size_mb", "文件体积下限(MB)", "0")),
                            self.__col(2, self.__text("cooldown_min", "写盘冷却(分钟)", "10")),
                            self.__col(2, self.__text("max_pkgs", "单轮提交上限", "30")),
                            self.__col(2, self.__text("max_files", "单次扫描文件上限", "200000")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(12, {
                                "component": "VAlert",
                                "props": {
                                    "type": "info", "variant": "tonal",
                                    "text": ("判定「已整理」只用 MoviePilot 自己的整理记录："
                                             "有成功记录的文件跳过；只有失败记录的算未整理"
                                             "（会交给 MP 清理旧记录后重试）；没有记录的算未整理。"
                                             "整包提交是安全的 —— MP 会逐文件查历史，"
                                             "已整理过的集直接跳过，不会重复整理或重复刮削。"),
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
                                    "type": "warning", "variant": "tonal",
                                    "text": ("「试运行」默认**开启**：走 MP 的 preview 模式"
                                             "只算规划、不落盘、不改库。"
                                             "第一次请先看试运行结果里的规划路径是否合理，"
                                             "确认后再关闭它执行真实整理。"),
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
                                    "type": "error", "variant": "tonal",
                                    "text": ("「目标冲突时跳过」建议保持**开启**：整理前先按同一套规则"
                                             "规划一遍，若算出来的目标文件在媒体库里已经存在同名文件"
                                             "（库里早就有一版、下载目录躺着另一版），整包跳过。"
                                             "核对数据时实测 23 个候选包里有 10 包属于这种"
                                             "「同剧不同版」，真整理会覆盖库里已有集数 ——"
                                             "而被覆盖的旧文件是硬链接，覆盖后旧源文件会掉成"
                                             "单链接（nlink=1），属于不可逆损伤。"),
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
                                    "type": "error", "variant": "tonal",
                                    "text": ("「只在种子完成后整理」是「边下边整理」的根因防护："
                                             "包名能在下载器里匹配到、且进度不足 100% 的一律跳过。"
                                             "「写盘冷却」再兜一层 —— 包内最新修改时间距今不足 N 分钟的"
                                             "也跳过。两道同时生效，宁可晚一轮也不产生半成品条目。"),
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
            "cron": "0 4 * * *",
            "dirs": "",
            "dry_run": True,
            "only_completed": True,
            "scrape": True,
            "min_size_mb": 0,
            "cooldown_min": 10,
            "max_pkgs": 30,
            "max_files": 200000,
            "exclude_words": "",
            "include_risky": False,
            "skip_conflict": True,
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
            "dirs": self._dirs,
            "dry_run": self._dry_run,
            "only_completed": self._only_completed,
            "scrape": self._scrape,
            "min_size_mb": self._min_size_mb,
            "cooldown_min": self._cooldown_min,
            "max_pkgs": self._max_pkgs,
            "max_files": self._max_files,
            "exclude_words": self._exclude_words,
            "include_risky": self._include_risky,
            "skip_conflict": self._skip_conflict,
        })

    # ==================================================================
    # 详情页
    # ==================================================================
    def get_page(self) -> Optional[List[dict]]:
        last = self.get_data("last") or {}
        history = self.get_data("history") or []

        scan_btn = {
            "component": "VBtn",
            "props": {"prepend-icon": "mdi-magnify-scan", "variant": "tonal",
                      "color": "secondary", "class": "mr-2"},
            "text": "扫描（试运行）",
            "events": {"click": {"api": f"plugin/{self.__class__.__name__}/run"
                                        f"?mode=scan&apikey={settings.API_TOKEN}",
                                 "method": "post"}},
        }
        apply_btn = {
            "component": "VBtn",
            "props": {"prepend-icon": "mdi-play-circle-outline", "variant": "tonal",
                      "color": "primary"},
            "text": "执行补整理",
            "events": {"click": {"api": f"plugin/{self.__class__.__name__}/run"
                                        f"?mode=apply&apikey={settings.API_TOKEN}",
                                 "method": "post"}},
        }

        if not last:
            return [
                {
                    "component": "div",
                    "props": {"class": "d-flex align-center"},
                    "content": [
                        {"component": "h2", "props": {"class": "page-title m-0"},
                         "text": "存量补整理"},
                        {"component": "VSpacer"},
                        scan_btn, apply_btn,
                    ],
                },
                {
                    "component": "div",
                    "text": ("暂无结果。先点「扫描（试运行）」看看有多少存量资源没进库 —— "
                             "试运行不会改动任何文件。"),
                    "props": {"class": "text-center mt-6 text-medium-emphasis"},
                },
            ]

        items = [{
            "root": (it.get("root") or "").rsplit("/", 1)[-1],
            "name": it.get("name") or "-",
            "files": f"{it.get('files', 0)}/{it.get('total_files', 0)}",
            "gb": it.get("gb", 0),
            "progress": (f"{it.get('progress')}%" if it.get("progress") is not None
                         else "-"),
            "titles": "、".join(it.get("titles") or []) or "-",
            "state": {"preview": "试运行", "ok": "成功", "fail": "失败",
                      "pending": "待处理", "in_library": "已在库",
                      "shared": "有其它链接"}.get(
                          it.get("state"), it.get("state")),
            "msg": (it.get("msg") or "")[:160],
        } for it in (last.get("items") or [])]

        summary = [
            self.__stat("扫描目录", f"{last.get('roots', 0)} 个"),
            self.__stat("扫描文件", f"{last.get('scanned_files', 0)} 个"),
            self.__stat("待补整理包", f"{last.get('candidates', 0)} 个", "orange"),
            self.__stat("待补整理文件", f"{last.get('files', 0)} 个", "orange"),
            self.__stat("待补整理体积", f"{last.get('gb', 0)} GB", "orange"),
            self.__stat("已在库(缺记录)", f"{last.get('in_library', 0)} 个", "brown"),
            self.__stat("有其它链接(跳过)", f"{last.get('shared', 0)} 个", "brown"),
            self.__stat("已提交", f"{last.get('submitted', 0)} 个", "blue"),
            self.__stat("成功", f"{last.get('ok', 0)} 个", "green"),
            self.__stat("失败", f"{last.get('fail', 0)} 个",
                        "red" if last.get("fail") else "grey"),
        ]

        page = [
            {
                "component": "div",
                "props": {"class": "d-flex align-center mb-2"},
                "content": [
                    {"component": "h2", "props": {"class": "page-title m-0"},
                     "text": "存量补整理"},
                    {"component": "span",
                     "props": {"class": "text-medium-emphasis text-body-2 mx-3"},
                     "text": (f"最近：{last.get('time', '-')}"
                              f"（{'试运行' if last.get('dry_run') else '实际整理'}）"
                              + ("｜运行中 ..." if self._running else ""))},
                    {"component": "VSpacer"},
                    scan_btn, apply_btn,
                ],
            },
            {"component": "VRow", "content": summary},
        ]

        if self._progress:
            page.append({
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "class": "mt-2",
                          "text": self._progress},
            })

        if last.get("dry_run"):
            page.append({
                "component": "VAlert",
                "props": {"type": "warning", "variant": "tonal", "class": "mt-2",
                          "text": ("下面这份是**试运行**结果，磁盘上没有任何改动。"
                                   "确认规划无误后，到配置页关闭「试运行」再点「执行补整理」。")},
            })

        if last.get("in_library") or last.get("shared"):
            page.append({
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "class": "mt-2",
                          "text": (f"另有 {last.get('in_library', 0)} 个包"
                                   f"（{last.get('in_library_files', 0)} 个文件）"
                                   f"**文件已经在媒体库**，只是整理记录被清理插件删了；"
                                   f"{last.get('shared', 0)} 个包"
                                   f"（{last.get('shared_files', 0)} 个文件）"
                                   f"**还有别的硬链接**，通常意味着同片的另一个版本已在库。"
                                   f"这两类都默认跳过 —— 再整理会在库里多出重复条目。"
                                   f"确需强行整理请在配置页打开「强行整理已排除项」。")},
            })

        if items:
            page.append({
                "component": "VRow",
                "content": [{
                    "component": "VCol",
                    "props": {"cols": 12},
                    "content": [{
                        "component": "VDataTableVirtual",
                        "props": {
                            "class": "text-sm",
                            "headers": [
                                {"title": "下载目录", "key": "root", "sortable": True,
                                 "width": "110"},
                                {"title": "资源包", "key": "name", "sortable": False},
                                {"title": "待整理/总", "key": "files", "sortable": True,
                                 "width": "110"},
                                {"title": "体积GB", "key": "gb", "sortable": True,
                                 "width": "90"},
                                {"title": "种子进度", "key": "progress", "sortable": False,
                                 "width": "90"},
                                {"title": "识别结果", "key": "titles", "sortable": False},
                                {"title": "结果", "key": "state", "sortable": False,
                                 "width": "80"},
                                {"title": "说明", "key": "msg", "sortable": False},
                            ],
                            "items": items,
                            "height": "32rem",
                            "density": "compact",
                            "fixed-header": True,
                            "hover": True,
                        },
                    }],
                }],
            })
        else:
            page.append({
                "component": "div",
                "text": "没有发现需要补整理的资源包 ✅（下载目录里都有整理记录）",
                "props": {"class": "text-center mt-6 text-medium-emphasis"},
            })

        if history:
            page.append({
                "component": "VRow",
                "content": [{
                    "component": "VCol",
                    "props": {"cols": 12},
                    "content": [
                        {"component": "h3", "props": {"class": "text-h6 mt-4 mb-2"},
                         "text": "运行历史"},
                        {
                            "component": "VDataTableVirtual",
                            "props": {
                                "class": "text-sm",
                                "headers": [
                                    {"title": "时间", "key": "time", "sortable": False},
                                    {"title": "模式", "key": "mode", "sortable": False,
                                     "width": "90"},
                                    {"title": "试运行", "key": "dry_run", "sortable": False,
                                     "width": "90"},
                                    {"title": "候选", "key": "candidates", "sortable": True,
                                     "width": "80"},
                                    {"title": "文件", "key": "files", "sortable": True,
                                     "width": "90"},
                                    {"title": "GB", "key": "gb", "sortable": True,
                                     "width": "90"},
                                    {"title": "提交", "key": "submitted", "sortable": True,
                                     "width": "80"},
                                    {"title": "成功", "key": "ok", "sortable": True,
                                     "width": "80"},
                                    {"title": "失败", "key": "fail", "sortable": True,
                                     "width": "80"},
                                ],
                                "items": history,
                                "height": "14rem",
                                "density": "compact",
                                "fixed-header": True,
                                "hover": True,
                            },
                        },
                    ],
                }],
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
            "content": [{
                "component": "VCard",
                "props": card_props,
                "content": [{
                    "component": "div",
                    "props": {"class": "text-caption"},
                    "text": title,
                }, {
                    "component": "div",
                    "props": {"class": "text-h6"},
                    "text": str(value),
                }],
            }],
        }

    # ==================================================================
    # 工具
    # ==================================================================
    @staticmethod
    def __norm(path: str) -> str:
        if not path:
            return ""
        return posixpath.normpath(str(path).replace("\\", "/"))

    @staticmethod
    def __exclude_reason(pkg: dict) -> str:
        state = pkg.get("state")
        if state == "in_library":
            return (f"文件已在媒体库（{pkg.get('in_lib', 0)} 个同 inode），"
                    f"只是整理记录缺失 —— 再整理会在库里出现第二个条目")
        if state == "shared":
            return (f"{pkg.get('shared', 0)} 个文件存在其它硬链接，"
                    f"同片另一版本大概率已在库 —— 强行整理会多出一个版本条目")
        return ""

    @staticmethod
    def __norm_progress(value) -> Optional[float]:
        """把下载器的进度统一成 0~1。

        各家量纲不一样：qBittorrent 返回 0~1（`progress`），
        Transmission 返回 0~100（`percentDone * 100`）。不归一化的话，
        tr 里下了一半的种子会被当成「已完成」直接放进整理队列。
        """
        try:
            p = float(value)
        except Exception:
            return None
        if p > 1.0:
            p = p / 100.0
        return max(0.0, min(1.0, p))

    @staticmethod
    def __to_int(value, default: int) -> int:
        try:
            return int(str(value).strip())
        except Exception:
            return default
