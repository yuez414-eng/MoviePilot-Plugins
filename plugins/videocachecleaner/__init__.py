# -*- coding: utf-8 -*-
"""
VideoCacheCleaner —— 绿联影视中心（com.ugreen.videomgr）转码缓存清理插件

== 为什么要这个插件 ==
绿联 UGOS 的「影视中心」在非原画播放时会把源视频实时转成 HLS 分片缓存，落在每个
存储卷根目录下的隐藏目录里。**两个目录都要清**：
  - `/volume1..5/@video/<32位md5>/app/h264_*.ts` —— 旧客户端 / APP 内转码；
  - `/volume1..5/@video_web/<32位md5>/...`       —— 新版网页播放器（HLS 分片）。
只清 @video 会漏掉网页播放那一路，实测 @video_web 往往比 @video 还大。
这些缓存：
  - 文件管理器不可见（`@` 前缀目录被 UGOS 隐藏）；
  - 边播边转、断点续转、**无上限、无自动清理**；
  - 实测增速约 2.6~4 GB/天，几天就能把 system 卷 / 数据卷吃到 100% 满；
  - 属主 root:root 且权限 0700，SSH 普通账号 `du`/`rm` 都进不去。

过去只能 SSH 上去 `docker run --rm -v /volumeN/@video:/v alpine sh -c 'find /v -mindepth 1 -delete'`
手动清，5 天清 4 次。本插件把这个动作固化进 MoviePilot，定时 + 手动触发，
每次清理记录各卷「清理前/后大小、文件数、释放空间」并写入详情页。

== 实现要点 ==
1. MoviePilot 容器只挂载了 `HDD_02/03/04` 子目录、**没有挂载卷根**，
   所以插件进程直接 `ls /volumeN/@video` 是看不到的。
2. 但 MoviePilot 容器挂载了宿主 `/var/run/docker.sock`（只读），
   因此本插件通过 Docker SDK 连宿主 Docker Engine，临时起一个 `alpine` 容器、
   把 `/volumeN/@video` 挂进去以 root 身份清理 —— 与手工 `docker run` 完全等价。
3. 只删 `@video` 与 `@video_web` 里的分片文件与空目录，不碰 `@thumbnail` / `@home` /
   `@search`；转码缓存删掉后下次播放会自动重建，零数据风险。

== 已知边界 ==
- 依赖 `docker` SDK 与 `alpine:latest` 镜像（本机已装）。
- 若某卷没有 `@video` / `@video_web` 目录，docker bind mount 会创建空目录并删除 0 个
   文件，无副作用（该目录本应存在）。
- 只清理「转码缓存」，不做容量其它归置。
"""
import datetime
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType

# 默认清理的卷（可配，逗号分隔）
_DEFAULT_VOLUMES = "1,2,3,4,5"
# 缓存目录名
_CACHE_DIRS = ("@video", "@video_web")
# 清理镜像
_IMAGE = "alpine:latest"


class VideoCacheCleaner(_PluginBase):
    # ---- 插件元信息 ----
    plugin_name = "转码缓存清理"
    plugin_desc = ("定时清理绿联影视中心产生的 HLS 转码缓存。绿联会在每卷根目录的隐藏目录里"
                   "边播边转、无上限、无自动清理 —— 旧客户端/APP 放 `@video`，新版网页播放器放 "
                   "`@video_web`，实测 2.6~4 GB/天，几天就能把数据卷吃到 100% 满。插件通过 "
                   "MoviePilot 容器已挂载的 docker.sock 临时起 alpine 容器、以 root 身份删除这两个"
                   "目录下的分片与空目录，等价于手工 `docker run --rm -v /volumeN/@video_web:/v "
                   "alpine sh -c 'find /v -mindepth 1 -delete'`。可定时 + 手动触发，每次按卷汇总"
                   "清理前后大小/文件数/释放空间写入详情页。只删转码缓存，不碰 "
                   "@thumbnail/@home/@search；删除后下次播放自动重建，零数据风险。")
    plugin_version = "1.0.1"
    plugin_author = "spizmm"
    plugin_icon = ("https://raw.githubusercontent.com/yuez414-eng/MoviePilot-Plugins/"
                   "main/icons/videocachecleaner.png")
    author_url = ""
    plugin_config_prefix = "VideoCacheCleaner_"
    plugin_order = 51
    auth_level = 1

    # ---- 运行期状态 ----
    _enable: bool = False
    _notify: bool = True
    _onlyonce: bool = False
    _cron: str = "0 4 * * *"
    _volumes: str = _DEFAULT_VOLUMES

    # ==================================================================
    # 生命周期
    # ==================================================================
    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enable = bool(config.get("enable", False))
        self._notify = bool(config.get("notify", True))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._cron = (config.get("cron") or "0 4 * * *").strip()
        self._volumes = (config.get("volumes") or _DEFAULT_VOLUMES).strip()

        if self._onlyonce:
            logger.info("【转码缓存清理】立即运行一次")
            import threading
            threading.Timer(3, self.__task).start()
            self._onlyonce = False
            self.__update_config()

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
                "summary": "立即清理转码缓存",
                "description": "手动触发一次 @video 转码缓存清理，结果写入插件详情页",
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enable and self._cron:
            try:
                trigger = CronTrigger.from_crontab(self._cron)
            except Exception as e:
                logger.error(f"【转码缓存清理】cron 表达式无效：{self._cron} - {e}")
                return []
            return [
                {
                    "id": "VideoCacheCleaner",
                    "name": "转码缓存清理",
                    "trigger": trigger,
                    "func": self.__task,
                    "kwargs": {},
                }
            ]
        return []

    def stop_service(self):
        # get_service 返回的任务由 MoviePilot 统一调度，插件自身无需持有 scheduler
        pass

    # ==================================================================
    # 手动触发 API
    # ==================================================================
    def __api_run(self) -> dict:
        try:
            summary = self.__task()
            return {"success": True, "message": "清理完成", "data": summary}
        except Exception as e:
            logger.error(f"【转码缓存清理】手动触发失败：{e}")
            return {"success": False, "message": f"清理失败：{e}"}

    # ==================================================================
    # 核心任务
    # ==================================================================
    def __task(self) -> dict:
        import docker

        vols = self.__parse_volumes(self._volumes)
        if not vols:
            logger.warning("【转码缓存清理】未配置任何卷，跳过")
            return {"total_released": 0, "volumes": []}

        client = docker.DockerClient(base_url="unix:///var/run/docker.sock")
        # 确认清理镜像可用
        try:
            client.images.get(_IMAGE)
        except Exception:
            logger.warning(f"【转码缓存清理】镜像 {_IMAGE} 不存在，尝试拉取")
            try:
                client.images.pull(_IMAGE)
            except Exception as e:
                logger.error(f"【转码缓存清理】拉取镜像失败：{e}")
                raise

        results: List[dict] = []
        total_files = 0
        total_bytes = 0

        for v in vols:
            v_before = v_after = v_files = 0
            v_errs = []
            for cache_dir in _CACHE_DIRS:
                host = f"/volume{v}/{cache_dir}"
                # 容器内一次完成：统计 → 删除 → 复统计 → 输出 before_kb files after_kb
                cmd = (
                    "sh -c '"
                    "b=$(du -sk /v 2>/dev/null | cut -f1); "
                    "n=$(find /v -mindepth 1 -type f 2>/dev/null | wc -l); "
                    "find /v -mindepth 1 -type f -delete 2>/dev/null; "
                    "find /v -mindepth 1 -type d -empty -delete 2>/dev/null; "
                    "a=$(du -sk /v 2>/dev/null | cut -f1); "
                    "echo \"$b $n $a\"'"
                )
                try:
                    out = client.containers.run(
                        _IMAGE,
                        command=cmd,
                        volumes={host: {"bind": "/v", "mode": "rw"}},
                        remove=True,
                        stdout=True,
                        stderr=False,
                    )
                    text = (out or b"").decode("utf-8", "replace").strip()
                    parts = text.split()
                    before_kb = int(parts[0]) if parts and parts[0].isdigit() else 0
                    files = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                    after_kb = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
                except Exception as e:
                    logger.error(f"【转码缓存清理】volume{v}/{cache_dir} 清理失败：{e}")
                    v_errs.append(f"{cache_dir}: {e}")
                    continue

                v_before += before_kb
                v_after += after_kb
                v_files += files
                logger.info(
                    f"【转码缓存清理】volume{v}/{cache_dir} 清理 {files} 个文件，"
                    f"释放 {max(before_kb - after_kb, 0) / 1024 / 1024:.2f} GB"
                )

            released_kb = max(v_before - v_after, 0)
            total_files += v_files
            total_bytes += released_kb * 1024
            results.append({
                "volume": v, "ok": not v_errs, "files": v_files,
                "before_gb": round(v_before / 1024 / 1024, 2),
                "after_gb": round(v_after / 1024 / 1024, 2),
                "released_gb": round(released_kb / 1024 / 1024, 2),
                "error": "; ".join(v_errs),
            })

        summary = {
            "total_files": total_files,
            "total_released": round(total_bytes / 1024 / 1024 / 1024, 2),
            "volumes": results,
            "time": datetime.datetime.now(
                tz=pytz.timezone(settings.TZ)).strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.save_data("last", summary)

        if self._notify and total_files:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="转码缓存清理完成",
                text=f"清理 {total_files} 个分片，释放 {summary['total_released']} GB",
            )
        logger.info(
            f"【转码缓存清理】本轮共清理 {total_files} 个文件，"
            f"释放 {summary['total_released']} GB"
        )
        return summary

    # ==================================================================
    # 配置表单
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
                            self.__col(2, self.__switch("notify", "清理后通知")),
                            self.__col(2, self.__switch("onlyonce", "立即运行一次")),
                            self.__col(6, self.__text("volumes", "清理的卷", "1,2,3,4,5")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(6, self.__cron("cron", "定时执行周期", "0 4 * * *")),
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
                                    "text": ("清理对象是绿联影视中心在各卷根目录下的两个隐藏缓存目录："
                                             "`/volume1..5/@video`（旧客户端转码）与 "
                                             "`/volume1..5/@video_web`（新版网页播放器分片），"
                                             "两者都是 HLS 转码缓存。"
                                             "插件通过 MoviePilot 容器挂载的 docker.sock 起 alpine 容器、"
                                             "以 root 身份删除，等价于手工 docker run。"
                                             "只删这两个目录的分片与空目录，不碰 @thumbnail/@home/@search；"
                                             "缓存删除后下次播放会自动重建，零风险。"),
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
                                    "text": ("依赖 `docker` SDK 与 `alpine:latest` 镜像（本机已装）。"
                                             "若 MoviePilot 容器未挂载 `/var/run/docker.sock`，"
                                             "插件会因连不上 Docker Engine 而失败，届时请为容器补挂该 socket。"),
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
            "volumes": _DEFAULT_VOLUMES,
            "cron": "0 4 * * *",
        }

    def get_page(self) -> Optional[List[dict]]:
        last = self.get_data("last")

        run_btn = {
            "component": "VBtn",
            "props": {"prepend-icon": "mdi-delete-sweep", "variant": "tonal",
                      "color": "primary"},
            "text": "立即清理",
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
                         "text": "转码缓存清理"},
                        {"component": "VSpacer"},
                        run_btn,
                    ],
                },
                {
                    "component": "div",
                    "text": "暂无清理记录，点右上角「立即清理」或到配置页打开「立即运行一次」。",
                    "props": {"class": "text-center mt-6 text-medium-emphasis"},
                },
            ]

        items = []
        for it in (last.get("volumes") or []):
            items.append({
                "卷": f"volume{it.get('volume')}",
                "状态": "成功" if it.get("ok") else "失败",
                "文件数": it.get("files"),
                "清理前(GB)": it.get("before_gb"),
                "清理后(GB)": it.get("after_gb"),
                "释放(GB)": it.get("released_gb"),
                "错误": it.get("error") or "-",
            })

        summary_alerts = [
            {
                "component": "div",
                "props": {"class": "d-flex align-center"},
                "content": [
                    {"component": "h2", "props": {"class": "page-title m-0"},
                     "text": "转码缓存清理"},
                    {"component": "VSpacer"},
                    run_btn,
                ],
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "success",
                    "variant": "tonal",
                    "text": (f"上次清理：{last.get('time', '-')}　共清理 "
                             f"{last.get('total_files', 0)} 个分片，"
                             f"释放 {last.get('total_released', 0)} GB"),
                },
            },
            {
                "component": "VTable",
                "props": {
                    "headers": [
                        {"title": "卷", "key": "卷"},
                        {"title": "状态", "key": "状态"},
                        {"title": "文件数", "key": "文件数"},
                        {"title": "清理前(GB)", "key": "清理前(GB)"},
                        {"title": "清理后(GB)", "key": "清理后(GB)"},
                        {"title": "释放(GB)", "key": "释放(GB)"},
                        {"title": "错误", "key": "错误"},
                    ],
                    "items": items,
                },
            },
        ]
        return summary_alerts

    # ==================================================================
    # helper
    # ==================================================================
    @staticmethod
    def __parse_volumes(raw: str) -> List[int]:
        vols: List[int] = []
        for part in (raw or "").split(","):
            part = part.strip()
            if part.isdigit():
                vols.append(int(part))
        return vols

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
            "volumes": self._volumes,
        })
