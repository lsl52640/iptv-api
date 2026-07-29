import asyncio
import copy
import datetime
import gzip
import os
import pickle
import sys
from time import time
from typing import Callable, Optional, Any

import pytz
from tqdm import tqdm

import utils.constants as constants
import utils.frozen as frozen
from updates.epg import get_epg
from updates.epg.tools import write_to_xml, compress_to_gz
from updates.subscribe import get_channels_by_subscribe_urls
from utils.aggregator import ResultAggregator
from utils.channel import get_channel_items, append_total_data, test_speed
from utils.config import config
from utils.speed import clear_cache
from utils.tools import (
    get_pbar_remaining,
    process_nested_dict,
    format_interval,
    check_ipv6_support,
    get_urls_from_file,
    get_version_info,
    get_urls_len,
    get_public_url,
    parse_times,
    to_serializable,
    get_subscribe_entries,
    count_disabled_urls,
)
from utils.types import CategoryChannelData
from utils.whitelist import load_whitelist_maps

ProgressCallback = Callable[..., Any]


class UpdateSource:
    def __init__(self):
        self.whitelist_maps = None
        self.blacklist = None

        self.update_progress: Optional[ProgressCallback] = None
        self.run_ui = False

        self.tasks: list[asyncio.Task] = []

        self.channel_items: CategoryChannelData = {}
        self.channel_names: list[str] = []

        self.subscribe_result = {}
        self.epg_result = {}

        self.channel_data: CategoryChannelData = {}

        self.pbar: Optional[tqdm] = None
        self.total = 0
        self.start_time = None

        self.stop_event: Optional[asyncio.Event] = None
        self.ipv6_support = False
        self.now = None

        self.aggregator: Optional[ResultAggregator] = None

    # ----------------------------
    # progress / pbar
    # ----------------------------
    def pbar_update(self, name: str = "", item_name: str = "", count: int = 1):
        if not self.pbar:
            return
        if self.pbar.n < self.total:
            self.pbar.update(min(max(1, count), self.total - self.pbar.n))
            remaining_total = self.total - self.pbar.n
            remaining_time = get_pbar_remaining(n=self.pbar.n, total=self.total, start_time=self.start_time)
            if self.update_progress:
                self.update_progress(
                    f"正在进行{name}，剩余{remaining_total}个{item_name}，预计完成剩余时间：{remaining_time}",
                    int((self.pbar.n / self.total) * 100),
                )

    # ----------------------------
    # IO: cache
    # ----------------------------
    def _load_cache(self) -> dict:
        if not (config.open_history and os.path.exists(constants.cache_path)):
            return {}
        try:
            with gzip.open(constants.cache_path, "rb") as f:
                return pickle.load(f) or {}
        except Exception:
            return {}

    def _save_cache(self, cache_result: dict):
        serializable = to_serializable(cache_result or {})
        cache_dir = os.path.dirname(constants.cache_path)
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
        with gzip.open(constants.cache_path, "wb") as f:
            pickle.dump(serializable, f)

    # ----------------------------
    # stage 1: prepare
    # ----------------------------
    def _prepare_channel_data(self):
        self.whitelist_maps = load_whitelist_maps(constants.whitelist_path)
        self.blacklist = get_urls_from_file(constants.blacklist_path, pattern_search=False)
        self.channel_items = get_channel_items(self.whitelist_maps, self.blacklist)
        self.channel_data = {}

        self.channel_names = [
            name for channel_obj in self.channel_items.values() for name in channel_obj.keys()
        ]

        if config.open_history and os.path.exists(constants.frozen_path):
            frozen.load(constants.frozen_path)

    # ----------------------------
    # stage 2: fetch subscribe/epg (concurrent)
    # ----------------------------
    async def _fetch_subscribe(self, channel_names: list[str], epg_urls_out: set = None):
        whitelist_entries, default_entries = get_subscribe_entries(constants.subscribe_path)
        disabled_count = count_disabled_urls(constants.subscribe_path)

        seen = set()
        subscribe_entries = []
        for e in (whitelist_entries + default_entries):
            url = e['url'] if isinstance(e, dict) else e
            if url in seen:
                continue
            seen.add(url)
            subscribe_entries.append(e)

        print(
            f"✅ 订阅源数量：{len(default_entries)}，订阅白名单数量：{len(whitelist_entries)}，停用地址数量：{disabled_count}，有效总数量：{len(subscribe_entries)}",
            flush=True,
        )

        if not subscribe_entries:
            print(f"❌ 没有找到有效的订阅地址，请检查文件：{constants.subscribe_path}，需要添加订阅地址后才能获取订阅源！", flush=True)
            return {}

        whitelist_urls = [e['url'] for e in whitelist_entries]

        return await get_channels_by_subscribe_urls(
            subscribe_entries,
            names=channel_names,
            whitelist=whitelist_urls,
            callback=self.update_progress,
            epg_urls_out=epg_urls_out,
        )

    async def _fetch_epg(self, channel_names: list[str], extra_entries: list = None):
        return await get_epg(channel_names, callback=self.update_progress, extra_entries=extra_entries)

    async def visit_page(self, channel_names: list[str] = None):
        """
        Visits subscribe and epg pages concurrently to fetch data.
        """
        channel_names = channel_names or []
        open_subscribe = config.open_method.get("subscribe")
        open_epg = config.open_method.get("epg")

        if open_subscribe and open_epg and config.open_subscribe_epg:
            discovered_epg_urls: set[str] = set()
            try:
                self.subscribe_result = await self._fetch_subscribe(channel_names, epg_urls_out=discovered_epg_urls)
            except Exception as e:
                print(f"subscribe_result failed: {e}", flush=True)
                self.subscribe_result = {}
            try:
                self.epg_result = await self._fetch_epg(channel_names, extra_entries=sorted(discovered_epg_urls))
            except Exception as e:
                print(f"epg_result failed: {e}", flush=True)
                self.epg_result = {}
            return

        cors: list[tuple[str, asyncio.Future]] = []
        if open_subscribe:
            cors.append(("subscribe_result", asyncio.create_task(self._fetch_subscribe(channel_names))))
        if open_epg:
            cors.append(("epg_result", asyncio.create_task(self._fetch_epg(channel_names))))

        if not cors:
            return

        results = await asyncio.gather(*(c for _, c in cors), return_exceptions=True)
        for (attr, _), res in zip(cors, results):
            if isinstance(res, Exception):
                print(f"{attr} failed: {res}", flush=True)
                setattr(self, attr, {})
            else:
                setattr(self, attr, res)

    def _write_epg_files_if_needed(self):
        if not self.epg_result:
            return
        write_to_xml(self.epg_result, constants.epg_result_path)
        compress_to_gz(constants.epg_result_path, constants.epg_gz_result_path)

    # ----------------------------
    # stage 3: aggregator lifecycle
    # ----------------------------
    async def _start_aggregator(self, cache: dict):
        self.aggregator = ResultAggregator(
            base_data=self.channel_data,
            first_channel_name=self.channel_names[0] if self.channel_names else None,
            ipv6_support=self.ipv6_support,
            write_interval=10.0,
            flush_debounce=2.0,
            min_items_before_flush=max(25, config.urls_limit),
            result=cache,
        )
        await self.aggregator.start()

    async def _stop_aggregator(self):
        if self.aggregator:
            aggregator = self.aggregator
            try:
                await aggregator.stop()
                return aggregator.result
            finally:
                self.aggregator = None
        return {}

    # ----------------------------
    # stage 4: speed test
    # ----------------------------
    async def _run_speed_test(self) -> CategoryChannelData:
        """
        Run speed test on the channel data and return the test results.
        """
        test_data = {
            category: copy.deepcopy(items)
            for category, items in self.channel_data.items()
            if category != "♻️未匹配频道"
        }
        urls_total = get_urls_len(test_data)

        process_nested_dict(
            test_data,
            seen=set(),
            filter_host=config.speed_test_filter_host,
            ipv6_support=self.ipv6_support,
        )
        self.total = get_urls_len(test_data)

        print(f"总接口数量: {urls_total}, 需要进行测速的接口数量: {self.total}")

        if self.total <= 0:
            self.aggregator.is_last = True
            return {}
            self.update_progress(
                f"🚀 正在进行测速, 总接口数量: {urls_total}, 需要进行测速的接口数量: {self.total}",
                0,
            )

        self.start_time = time()
        self.pbar = tqdm(
            total=self.total,
            desc="🚀 测速",
            file=sys.stdout,
            mininterval=1.0,
            miniters=1,
            dynamic_ncols=False,
        )
        try:
            result = await test_speed(
                test_data,
                ipv6=self.ipv6_support,
                callback=lambda count=1: self.pbar_update(
                    name="🚀 测速",
                    item_name="接口",
                    count=count,
                ),
                on_task_complete=self.aggregator.add_item,
            )
            self.aggregator.is_last = True
            return result
        finally:
            if self.pbar:
                self.pbar.close()
                self.pbar = None

    # ----------------------------
    # stage 5: ui final notify
    # ----------------------------
    def _notify_ui_finished(self, main_start_time: float):
        if not self.run_ui:
            return

        tip = f"🥳 更新完成！总耗时：{format_interval(time() - main_start_time)}"

        if self.update_progress:
            self.update_progress(
                tip,
                100,
                finished=True,
                url=None,
                now=self.now,
            )

    # ----------------------------
    # main flow
    # ----------------------------
    async def main(self):
        try:
            main_start_time = time()
            performance = config.performance_settings
            print(
                f"⚙️ 性能模式: {performance.requested_mode} → {performance.resolved_mode}，可用资源: {performance.cpu_count} CPU / {performance.memory_gb} GB，并发: 测速 {performance.speed_test_concurrency}、媒体探测 {performance.probe_concurrency}、源抓取 {performance.fetch_workers}",
                flush=True,
            )

            self._prepare_channel_data()

            if not self.channel_names:
                print(f"❌ 模板中没有任何频道名称！请检查文件：{config.source_file}！", flush=True)
                self._notify_ui_finished(main_start_time)
                return

            await self.visit_page(self.channel_names)
            self.tasks = []
            self._write_epg_files_if_needed()

            append_total_data(
                self.channel_items.items(),
                self.channel_data,
                self.subscribe_result,
                self.whitelist_maps,
                self.blacklist,
            )

            cache = self._load_cache()

            await self._start_aggregator(cache)
            try:
                if config.open_speed_test:
                    clear_cache()
                    await self._run_speed_test()
                else:
                    self.aggregator.test_results = self.channel_data
                    self.aggregator.is_last = True

            finally:
                final_result = await self._stop_aggregator()
                if config.open_history:
                    self._save_cache(final_result)
                    frozen.save(constants.frozen_path)

            print(
                f"🥳 更新完成！总耗时：{format_interval(time() - main_start_time)}",
                flush=True,
            )
            self._notify_ui_finished(main_start_time)

        except asyncio.exceptions.CancelledError:
            print("更新已被取消！", flush=True)

    # ----------------------------
    # lifecycle control
    # ----------------------------
    async def start(self, callback=None):
        def default_callback(*args, **kwargs):
            pass

        self.update_progress = callback or default_callback
        self.run_ui = True if callback else False

        if not config.open_update:
            if self.run_ui:
                self.update_progress("⚠️ 更新功能已被禁用", 0, finished=True)
            return

        if self.run_ui:
            self.update_progress("🛒 正在检查当前网络是否支持IPv6...", 0)

        self.ipv6_support = config.ipv6_support or check_ipv6_support()

        await self.main()

    def stop(self):
        for task in self.tasks:
            task.cancel()
        self.tasks = []

        if self.pbar:
            self.pbar.close()
            self.pbar = None

        if self.stop_event:
            self.stop_event.set()


if __name__ == "__main__":
    info = get_version_info()
    print(f"⚡️ {info['name']} 版本: {info['version']} (构建时间: {info['build_time']})", flush=True)
    if not config.open_update:
        print("⚠️ 更新功能已被禁用", flush=True)
    else:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        update_source = UpdateSource()
        loop.run_until_complete(update_source.start())
