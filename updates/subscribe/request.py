from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from logging import INFO
from threading import Lock
from time import time
import sys

from tqdm.asyncio import tqdm_asyncio

import utils.constants as constants
from utils.channel import format_channel_name
from utils.config import config
from utils.requests.tools import get_soup_requests
from utils.retry import retry_func
from utils.tools import (
    get_pbar_remaining,
    get_name_value,
    get_m3u_epg_urls,
    get_logger,
    get_request_url_candidates,
    request_first,
    save_url_content, close_logger_handlers,
    disable_urls_in_file,
)


def _channel_item_key(item):
    return (
        item.get("url"),
        tuple(sorted((item.get("headers") or {}).items())),
        item.get("tvg_logo"),
        item.get("extra_info", ""),
    )


def _merge_channel_results(target, source, seen):
    for name, items in source.items():
        target_items = target.setdefault(name, [])
        name_seen = seen.setdefault(name, set())
        for item in items:
            key = _channel_item_key(item)
            if key in name_seen:
                continue
            name_seen.add(key)
            target_items.append(item)


async def get_channels_by_subscribe_urls(
        urls,
        names=None,
        whitelist=None,
        callback=None,
        epg_urls_out=None,
):
    """
    Get the channels by subscribe urls
    """
    normalized_names = {format_channel_name(name) for name in (names or []) if name}
    if whitelist:
        index_map = {u: i for i, u in enumerate(whitelist)}

        def sort_key(u):
            key = u['url'] if isinstance(u, dict) else u
            return index_map.get(key, len(whitelist))

        urls.sort(key=sort_key)
    subscribe_results = {}
    subscribe_urls_len = len(urls)
    pbar = tqdm_asyncio(
        total=subscribe_urls_len,
        desc="🔍 正在进行获取订阅源",
        file=sys.stdout,
        mininterval=1.0,
        miniters=1,
        dynamic_ncols=False,
    )
    start_time = time()
    mode_name = "订阅源"
    if callback:
        callback(
            "🔍 正在进行获取订阅源",
            0,
        )
    logger = get_logger(constants.unmatch_log_path, level=INFO, init=True)
    request_timeout = config.request_timeout
    open_headers = config.open_headers
    open_unmatch_category = config.open_unmatch_category
    open_auto_disable_source = config.open_auto_disable_source
    open_subscribe_epg = config.open_subscribe_epg
    disabled_urls = set()
    disabled_lock = Lock()
    discovered_epg_urls = set()
    epg_discover_lock = Lock()
    unmatched_logged = 0
    unmatched_lock = Lock()
    unmatched_log_limit = 10000

    def _mark_disabled(source_url: str, reason: str):
        if not open_auto_disable_source or not source_url:
            return
        with disabled_lock:
            disabled_urls.add(source_url)
        print(f"⛔ 订阅源地址已自动停用：{source_url}（{reason}）", flush=True)

    def process_subscribe_channels(subscribe_info: str | dict) -> defaultdict:
        nonlocal unmatched_logged
        subscribe_url = subscribe_info.get('url') if isinstance(subscribe_info, dict) else subscribe_info
        source_url = subscribe_info.get('source_url', subscribe_url) if isinstance(subscribe_info,
                                                                                   dict) else subscribe_url
        headers = subscribe_info.get('headers') if isinstance(subscribe_info, dict) else None
        channels = defaultdict(list)
        channel_seen = defaultdict(set)
        in_whitelist = whitelist and (subscribe_url in whitelist)
        disable_reason = None
        try:
            response = None
            try:
                candidates = get_request_url_candidates(subscribe_url)
                response = retry_func(
                    lambda: request_first(
                        candidates,
                        lambda u: get_soup_requests(u, timeout=request_timeout, headers_override=headers),
                    ),
                    name=subscribe_url,
                )
            except Exception as e:
                print(e, flush=True)
                disable_reason = "请求失败"
            if response:
                if hasattr(response, 'text'):
                    response.encoding = "utf-8"
                    content = response.text
                else:
                    content = str(response)
                if not content:
                    disable_reason = "内容为空"
                try:
                    save_url_content('subscribe', subscribe_url, content)
                except Exception:
                    pass
                if content:
                    m3u_type = True if "#EXTM3U" in content else False
                    if open_subscribe_epg and m3u_type:
                        found_epg_urls = get_m3u_epg_urls(content)
                        if found_epg_urls:
                            with epg_discover_lock:
                                discovered_epg_urls.update(found_epg_urls)
                    data = get_name_value(
                        content,
                        pattern=(
                            constants.multiline_m3u_pattern
                            if m3u_type
                            else constants.multiline_txt_pattern
                        ),
                        open_headers=open_headers if m3u_type else False
                    )
                    for item in data:
                        data_name = item.get("name", "").strip()
                        url = item.get("value", "").strip()
                        if data_name and url:
                            name = format_channel_name(data_name)
                            if normalized_names and name not in normalized_names:
                                with unmatched_lock:
                                    if unmatched_logged < unmatched_log_limit:
                                        logger.info(f"{data_name},{url}")
                                        unmatched_logged += 1
                                if not open_unmatch_category:
                                    continue
                            url_partition = url.partition("$")
                            url = url_partition[0]
                            info = url_partition[2]
                            item_headers = {**(headers or {}), **(item.get("headers") or {})}
                            value = {
                                "url": url,
                                "headers": item_headers or None,
                                "tvg_logo": item.get("tvg_logo") or None,
                                "extra_info": info
                            }
                            if in_whitelist:
                                value["origin"] = "whitelist"
                            key = _channel_item_key(value)
                            if key not in channel_seen[name]:
                                channel_seen[name].add(key)
                                channels[name].append(value)
                if not channels and not disable_reason:
                    disable_reason = "没有匹配到符合条件的值"
        except Exception as e:
            print(f"❌ {subscribe_url} 出错：{e}", flush=True)
            if not disable_reason:
                disable_reason = "请求失败"
        finally:
            if disable_reason:
                _mark_disabled(source_url, disable_reason)
            pbar.update()
            if callback:
                callback(
                    f"正在进行获取订阅源，剩余{subscribe_urls_len - pbar.n}个订阅源，预计完成剩余时间：{get_pbar_remaining(n=pbar.n, total=pbar.total, start_time=start_time)}",
                    int((pbar.n / subscribe_urls_len) * 100),
                )
        return channels

    with ThreadPoolExecutor(max_workers=config.performance_settings.fetch_workers) as executor:
        futures = [
            executor.submit(process_subscribe_channels, subscribe_url)
            for subscribe_url in urls
        ]
        subscribe_results = defaultdict(list)
        subscribe_seen = defaultdict(set)
        for future in futures:
            _merge_channel_results(subscribe_results, future.result(), subscribe_seen)
        pbar.close()
        active_count = len(urls)
        disabled_count = 0
        if disabled_urls:
            counts = disable_urls_in_file(constants.subscribe_path, disabled_urls)
            active_count = counts["active"]
            disabled_count = counts["disabled"]
        print(f"🆗 订阅源: ✅ 有效地址数量：{active_count}，⛔ 停用地址数量：{disabled_count}", flush=True)
        if epg_urls_out is not None and discovered_epg_urls:
            epg_urls_out.update(discovered_epg_urls)
            print(f"📡 从订阅源中发现 {len(discovered_epg_urls)} 个 EPG 地址", flush=True)
        close_logger_handlers(logger)
        return subscribe_results
