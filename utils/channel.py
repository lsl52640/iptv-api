import asyncio
import gzip
import hashlib
import math
import os
import pickle
import re
import tempfile
from collections import defaultdict, Counter, OrderedDict
from itertools import chain
from logging import INFO
from typing import cast

import utils.constants as constants
from utils.alias import Alias
from utils.config import config
from utils.ffmpeg import check_ffmpeg_installed_status
from utils.frozen import is_url_frozen, mark_url_bad, mark_url_good
from utils.ip_checker import IPChecker
from utils.speed import (
    create_speed_test_session,
    get_speed,
    get_speed_result,
    get_sort_result
)
from utils.tools import (
    format_name,
    get_name_value,
    check_url_by_keywords,
    get_total_urls,
    add_url_info,
    resource_path,
    get_name_urls_from_file,
    get_logger,
    get_datetime_now,
    get_url_host,
    check_ipv_type_match,
    convert_to_json_v1,
    custom_print,
    get_resolution_value,
    get_public_url,
    build_path_list,
    get_real_path,
    count_files_by_ext,
    close_logger_handlers,
    fast_get_ipv_type
)
from utils.types import ChannelData, OriginType, CategoryChannelData, WhitelistMaps
from utils.whitelist import is_url_whitelisted, get_whitelist_url, get_whitelist_total_count

channel_alias = Alias()
ip_checker = IPChecker()
location_list = config.location
isp_list = config.isp
open_supply = config.open_supply
open_filter_speed = config.open_filter_speed
min_speed = config.min_speed
open_filter_resolution = config.open_filter_resolution
min_resolution_value = config.min_resolution_value
resolution_speed_map = config.resolution_speed_map
open_history = config.open_history
open_local = config.open_local
retain_origin = ["whitelist", "hls"]

_TOTAL_URLS_CACHE_MAX_SIZE = 2048
_TOTAL_URLS_CACHE = OrderedDict()


class _LimitedLogger:
    def __init__(self, logger, limit):
        self.logger = logger
        self.limit = limit
        self.count = 0

    def info(self, *args, **kwargs):
        if self.count >= self.limit:
            return
        self.count += 1
        self.logger.info(*args, **kwargs)


def _build_total_urls_signature(info_list: list[ChannelData]) -> str:
    """
    Build a stable signature for a channel info list.
    """
    hasher = hashlib.sha1()
    for info in info_list or []:
        if not isinstance(info, dict):
            hasher.update(repr(info).encode("utf-8", errors="ignore"))
            hasher.update(b"\x1e")
            continue

        origin = info.get("origin") or ""
        extra_info = info.get("extra_info") or ""
        if origin not in retain_origin and not extra_info:
            extra_info = constants.origin_map.get(origin, "")

        hasher.update(
            "\x1f".join((
                str(info.get("id", "")),
                info.get("url") or "",
                origin,
                info.get("ipv_type") or "",
                extra_info,
            )).encode("utf-8", errors="ignore")
        )
        hasher.update(b"\x1e")

    return hasher.hexdigest()


def _get_total_urls_cached(
        info_list: list[ChannelData],
        ipv_type_prefer,
        origin_type_prefer,
        rtmp_type=None,
        apply_limit: bool = True,
) -> tuple:
    """
    Cached wrapper for `get_total_urls()`.
    """
    ipv_key = tuple(ipv_type_prefer or ())
    origin_key = tuple(origin_type_prefer or ())
    rtmp_key = tuple(rtmp_type or ())
    cache_key = (
        _build_total_urls_signature(info_list),
        ipv_key,
        origin_key,
        rtmp_key,
        bool(apply_limit),
        config.urls_limit,
    )
    cached = _TOTAL_URLS_CACHE.get(cache_key)
    if cached is not None:
        _TOTAL_URLS_CACHE.move_to_end(cache_key)
        return cached

    total_urls = tuple(get_total_urls(info_list, ipv_type_prefer, origin_type_prefer, rtmp_type, apply_limit))
    _TOTAL_URLS_CACHE[cache_key] = total_urls
    if len(_TOTAL_URLS_CACHE) > _TOTAL_URLS_CACHE_MAX_SIZE:
        _TOTAL_URLS_CACHE.popitem(last=False)
    return total_urls


def format_channel_data(url: str, origin: OriginType) -> ChannelData:
    """
    Format the channel data
    """
    url_partition = url.partition("$")
    url = url_partition[0]
    info = url_partition[2]
    if info and info.startswith("!"):
        origin = "whitelist"
        info = info[1:]
    return {
        "id": hash(url),
        "url": url,
        "host": get_url_host(url),
        "origin": cast(OriginType, origin),
        "ipv_type": None,
        "extra_info": info
    }


def check_channel_need_frozen(info) -> bool:
    """
    Check if the channel need to be frozen
    """
    delay = info.get("delay", 0)
    if delay == -1 or info.get("speed", 0) == 0:
        return True
    if info.get("resolution"):
        if get_resolution_value(info["resolution"]) < min_resolution_value:
            return True
    return False


def get_channel_data_from_file(channels, file, whitelist_maps, blacklist,
                               local_data=None, hls_data=None) -> CategoryChannelData:
    """
    Get the channel data from the file
    """
    current_category = ""
    matched_local_names = set()
    matched_hls_names = set()
    unmatch_category = "♻️未匹配频道"

    def append_unmatch_data(name: str, info_list: list):
        category_dict = channels[unmatch_category]
        if name not in category_dict:
            category_dict[name] = []
        existing_urls = {d.get("url") for d in category_dict.get(name, []) if d.get("url")}
        for item in info_list:
            if not item:
                continue
            url = item.get("url")
            if not url or url in existing_urls:
                continue
            category_dict[name].append(item)
            existing_urls.add(url)

    for line in file:
        line = line.strip()
        if "#genre#" in line:
            current_category = re.split(r"[，,]", line, maxsplit=1)[0]
        else:
            name_value = get_name_value(
                line, pattern=constants.demo_txt_pattern, check_value=False
            )
            if name_value and name_value[0]:
                name = name_value[0]["name"]
                url = name_value[0]["value"]
                category_dict = channels[current_category]
                first_time = name not in category_dict
                if first_time:
                    category_dict[name] = []
                existing_urls = {d.get("url") for d in category_dict.get(name, []) if d.get("url")}

                if first_time:
                    for whitelist_url in get_whitelist_url(whitelist_maps, name):
                        formatted = format_channel_data(whitelist_url, "whitelist")
                        if formatted["url"] not in existing_urls:
                            category_dict[name].append(formatted)
                            existing_urls.add(formatted["url"])

                    if hls_data and name in hls_data:
                        matched_hls_names.add(name)
                        for hls_url in hls_data[name]:
                            formatted = format_channel_data(hls_url, "hls")
                            if formatted["url"] not in existing_urls:
                                category_dict[name].append(formatted)
                                existing_urls.add(formatted["url"])

                    if open_local and local_data:
                        alias_names = channel_alias.get(name)
                        alias_names.update([name, format_name(name)])
                        for alias_name in alias_names:
                            if alias_name in local_data:
                                matched_local_names.add(alias_name)
                                for local_url in local_data[alias_name]:
                                    if not check_url_by_keywords(local_url, blacklist):
                                        local_url_origin: OriginType = "whitelist" if is_url_whitelisted(whitelist_maps,
                                                                                                         local_url,
                                                                                                         name) else "local"
                                        formatted = format_channel_data(local_url, local_url_origin)
                                        if formatted["url"] not in existing_urls:
                                            category_dict[name].append(formatted)
                                            existing_urls.add(formatted["url"])
                            elif alias_name.startswith("re:"):
                                raw_pattern = alias_name[3:]
                                try:
                                    pattern = re.compile(raw_pattern)
                                    for local_name in local_data:
                                        if re.match(pattern, local_name):
                                            matched_local_names.add(local_name)
                                            for local_url in local_data[local_name]:
                                                if not check_url_by_keywords(local_url, blacklist):
                                                    local_url_origin: OriginType = "whitelist" if is_url_whitelisted(
                                                        whitelist_maps,
                                                        local_url,
                                                        name) else "local"
                                                    formatted = format_channel_data(local_url, local_url_origin)
                                                    if formatted["url"] not in existing_urls:
                                                        category_dict[name].append(formatted)
                                                        existing_urls.add(formatted["url"])
                                except re.error:
                                    pass
                if url:
                    if is_url_whitelisted(whitelist_maps, url, name):
                        formatted = format_channel_data(url, "whitelist")
                        if formatted["url"] not in existing_urls:
                            category_dict[name].append(formatted)
                            existing_urls.add(formatted["url"])
                    elif open_local and not check_url_by_keywords(url, blacklist):
                        formatted = format_channel_data(url, "local")
                        if formatted["url"] not in existing_urls:
                            category_dict[name].append(formatted)
                            existing_urls.add(formatted["url"])

    if config.open_unmatch_category:
        if open_local and local_data:
            for local_name, local_urls in local_data.items():
                if local_name in matched_local_names:
                    continue
                unmatch_local_urls = [
                    format_channel_data(local_url, "whitelist" if is_url_whitelisted(whitelist_maps, local_url,
                                                                                     local_name) else "local")
                    for local_url in local_urls
                    if not check_url_by_keywords(local_url, blacklist)
                ]
                if unmatch_local_urls:
                    append_unmatch_data(local_name, unmatch_local_urls)


    return channels


def get_channel_items(whitelist_maps, blacklist) -> CategoryChannelData:
    """
    Get the channel items from the source file
    """
    user_source_file = resource_path(config.source_file)
    channels = defaultdict(lambda: defaultdict(list))
    hls_data = None
    local_paths = build_path_list(constants.local_dir_path)
    local_data = get_name_urls_from_file([get_real_path(constants.local_path)] + local_paths)
    whitelist_count = get_whitelist_total_count(whitelist_maps)
    blacklist_count = len(blacklist)
    channel_logo_count = count_files_by_ext(resource_path(constants.channel_logo_path), [config.logo_type])
    if whitelist_count:
        print(f"✅ 白名单接口规则数量：{whitelist_count}")
    if blacklist_count:
        print(f"✅ 黑名单接口规则数量：{blacklist_count}")
    if channel_logo_count:
        print(f"✅ 本地台标数量：{channel_logo_count}")

    if os.path.exists(user_source_file):
        with open(user_source_file, "r", encoding="utf-8") as file:
            channels = get_channel_data_from_file(
                channels, file, whitelist_maps, blacklist, local_data, hls_data
            )

    source_name_targets = defaultdict(list)
    for cate, data in channels.items():
        for name in data.keys():
            source_name_targets[format_channel_name(name)].append((cate, name))

    if config.open_history and os.path.exists(constants.cache_path):
        unmatched_history = defaultdict(list)

        def _append_history_items(channel_data, info_list):
            urls = [url for item in channel_data if (url := item.get("url"))]
            for info in info_list:
                if not info:
                    continue
                info_url = info.get("url")
                try:
                    if info.get("origin") in retain_origin or check_url_by_keywords(info_url, blacklist):
                        continue
                    if check_channel_need_frozen(info):
                        mark_url_bad(info_url, initial=True)
                        continue
                except Exception:
                    pass
                if info_url and info_url not in urls:
                    channel_data.append(info)
                    urls.append(info_url)

        try:
            with gzip.open(constants.cache_path, "rb") as file:
                old_result = pickle.load(file) or {}
                for cate, data in old_result.items():
                    for name, info_list in data.items():
                        targets = source_name_targets.get(format_channel_name(name))
                        if targets:
                            for target_cate, target_name in targets:
                                channel_data = channels[target_cate][target_name]
                                _append_history_items(channel_data, info_list)
                                if not channel_data:
                                    for info in info_list:
                                        old_result_url = info.get("url") if info else None
                                        if info and info.get(
                                                "origin") not in retain_origin and old_result_url and not check_url_by_keywords(
                                            old_result_url, blacklist):
                                            channel_data.append(info)
                        else:
                            unmatched_history[name].extend(info_list)
        except Exception as e:
            print(f"❌ 加载缓存文件出错：{e}")

        if unmatched_history and config.open_unmatch_category:
            unmatch_category = "♻️未匹配频道"
            for name, info_list in unmatched_history.items():
                append_data_to_info_data(
                    channels,
                    unmatch_category,
                    name,
                    info_list,
                    whitelist_maps=whitelist_maps,
                    blacklist=blacklist,
                    skip_validation=True,
                )
    return channels


def format_channel_name(name):
    """
    Format the channel name with sub and replace and lower
    """
    return channel_alias.get_primary(name)


def channel_name_is_equal(name1, name2):
    """
    Check if the channel name is equal
    """
    name1_format = format_channel_name(name1)
    name2_format = format_channel_name(name2)
    return name1_format == name2_format


def get_channel_results_by_name(name, data):
    """
    Get channel results from data by name
    """
    format_name = format_channel_name(name)
    results = data.get(format_name, [])
    return results


def get_channel_url(text):
    """
    Get the url from text
    """
    url = None
    url_search = constants.url_pattern.search(text)
    if url_search:
        url = url_search.group()
    return url


def init_info_data(data: dict, category: str, name: str) -> None:
    """
    Initialize channel info data structure if not exists
    """
    data.setdefault(category, {}).setdefault(name, [])


def append_data_to_info_data(
        info_data: dict,
        category: str,
        name: str,
        data: list,
        origin: str = None,
        whitelist_maps: WhitelistMaps = None,
        blacklist: list = None,
        ipv_type_data: dict = None,
        skip_validation: bool = False
) -> None:
    """
    Append channel data to total info data with deduplication and validation

    Args:
        info_data: The main data structure to update
        category: Category key for the data
        name: Name key within the category
        data: List of channel items to process
        origin: Default origin for items
        whitelist_maps: Maps of whitelist keywords
        blacklist: List of blacklist keywords
        ipv_type_data: Dictionary to cache IP type information
        skip_validation: If True, skip validation and directly append data
    """
    init_info_data(info_data, category, name)

    channel_list = info_data[category][name]
    existing_map = {info["url"]: idx for idx, info in enumerate(channel_list) if "url" in info}

    for item in data:
        try:
            channel_id = item.get("id") or hash(item["url"])
            raw_url = item.get("url")
            host = item.get("host") or (get_url_host(raw_url) if raw_url else None)
            date = item.get("date")
            delay = item.get("delay")
            speed = item.get("speed")
            resolution = item.get("resolution")
            url_origin = item.get("origin", origin)
            ipv_type = item.get("ipv_type")
            location = item.get("location")
            isp = item.get("isp")
            headers = item.get("headers")
            catchup = item.get("catchup")
            tvg_logo = item.get("tvg_logo")
            extra_info = item.get("extra_info", "")

            if not raw_url:
                continue

            normalized_url = raw_url
            if url_origin not in retain_origin:
                normalized_url = get_channel_url(raw_url)
                if not normalized_url:
                    continue
                if is_url_frozen(normalized_url):
                    continue
                if blacklist and check_url_by_keywords(normalized_url, blacklist):
                    continue

            if url_origin != "whitelist" and whitelist_maps and is_url_whitelisted(whitelist_maps, normalized_url,
                                                                                   name):
                url_origin = "whitelist"

            if skip_validation and url_origin not in retain_origin and not ipv_type:
                if ipv_type_data and host in ipv_type_data:
                    ipv_type = ipv_type_data[host]
                else:
                    ipv_type = fast_get_ipv_type(host)
                    if ipv_type_data is not None and host:
                        ipv_type_data[host] = ipv_type

            if normalized_url in existing_map:
                existing_idx = existing_map[normalized_url]
                existing_origin = channel_list[existing_idx].get("origin")
                if existing_origin != "whitelist" and url_origin == "whitelist":
                    channel_list[existing_idx] = {
                        "id": channel_id,
                        "url": normalized_url,
                        "host": host or get_url_host(normalized_url),
                        "date": date,
                        "delay": delay,
                        "speed": speed,
                        "resolution": resolution,
                        "origin": url_origin,
                        "ipv_type": ipv_type,
                        "location": location,
                        "isp": isp,
                        "headers": headers,
                        "catchup": catchup,
                        "tvg_logo": tvg_logo,
                        "extra_info": extra_info
                    }
                    continue
                else:
                    continue

            url = normalized_url
            supply = False

            if url_origin not in retain_origin:
                if not skip_validation:
                    if not ipv_type:
                        if ipv_type_data and host in ipv_type_data:
                            ipv_type = ipv_type_data[host]
                        else:
                            ipv_type = ip_checker.get_ipv_type(url)
                            if ipv_type_data is not None:
                                ipv_type_data[host] = ipv_type

                    if not check_ipv_type_match(ipv_type):
                        continue

                    if not location or not isp:
                        ip = ip_checker.get_ip(url)
                        if ip:
                            location, isp = ip_checker.find_map(ip)

                    if location and location_list and not any(item in location for item in location_list):
                        if not open_supply:
                            continue
                        supply = True

                    if isp and isp_list and not any(item in isp for item in isp_list):
                        if not open_supply:
                            continue
                        supply = True

            channel_list.append({
                "id": channel_id,
                "url": url,
                "host": host or get_url_host(url),
                "date": date,
                "delay": delay,
                "speed": speed,
                "resolution": resolution,
                "origin": url_origin,
                "ipv_type": ipv_type,
                "location": location,
                "isp": isp,
                "headers": headers,
                "catchup": catchup,
                "tvg_logo": tvg_logo,
                "extra_info": extra_info,
                "supply": supply
            })
            existing_map[url] = len(channel_list) - 1

        except Exception as e:
            print(f"❌ 追加频道数据错误：{e}")
            continue


def append_old_data_to_info_data(info_data, cate, name, data, whitelist_maps=None, blacklist=None, ipv_type_data=None):
    """
    Append old existed channel data to total info data
    """

    def append_and_print(items, origin, label):
        if items:
            append_data_to_info_data(
                info_data, cate, name, items,
                origin=origin if origin else None,
                whitelist_maps=whitelist_maps,
                blacklist=blacklist,
                ipv_type_data=ipv_type_data
            )
        items_len = len(items)
        if items_len > 0:
            print(f"{label}: {items_len}", end=", ")

    whitelist_data = [item for item in data if item["origin"] == "whitelist"]
    append_and_print(whitelist_data, "whitelist", "白名单")

    if open_local:
        local_data = [item for item in data if item["origin"] == "local"]
        append_and_print(local_data, "local", "本地源")



    if open_history:
        history_data = [item for item in data if item["origin"] not in ["hls", "local", "whitelist"]]
        append_and_print(history_data, None, "历史源")


def print_channel_number(data: CategoryChannelData, cate: str, name: str):
    """
    Print channel number
    """
    channel_list = data.get(cate, {}).get(name, [])
    print("IPv4:", len([channel for channel in channel_list if channel["ipv_type"] == "ipv4"]), end=", ")
    print("IPv6:", len([channel for channel in channel_list if channel["ipv_type"] == "ipv6"]), end=", ")
    print(
        "总计:",
        len(channel_list),
    )


def append_total_data(
        items,
        data,
        subscribe_result=None,
        whitelist_maps=None,
        blacklist=None,
):
    """
    Append all method data to total info data
    """
    items = list(items)
    total_result = [
        ("subscribe", subscribe_result),
    ]
    unmatch_category = "♻️未匹配频道"
    source_names = {
        format_channel_name(name)
        for cate, channel_obj in items
        if cate != unmatch_category
        for name in channel_obj.keys()
    }
    url_hosts_ipv_type = {}
    for obj in data.values():
        for value_list in obj.values():
            for value in value_list:
                if value_ipv_type := value.get("ipv_type", None):
                    url_hosts_ipv_type[get_url_host(value["url"])] = value_ipv_type
    for cate, channel_obj in items:
        if cate == unmatch_category:
            for name, old_info_list in channel_obj.items():
                if old_info_list:
                    append_data_to_info_data(
                        data,
                        cate,
                        name,
                        old_info_list,
                        whitelist_maps=whitelist_maps,
                        blacklist=blacklist,
                        ipv_type_data=url_hosts_ipv_type,
                        skip_validation=True,
                    )
            continue

        for name, old_info_list in channel_obj.items():
            print(f"{name}:", end=" ")
            if old_info_list:
                append_old_data_to_info_data(data, cate, name, old_info_list, whitelist_maps=whitelist_maps,
                                             blacklist=blacklist,
                                             ipv_type_data=url_hosts_ipv_type)
            for method, result in total_result:
                if config.open_method[method]:
                    name_results = get_channel_results_by_name(name, result)
                    append_data_to_info_data(
                        data, cate, name, name_results, origin=method, whitelist_maps=whitelist_maps,
                        blacklist=blacklist,
                        ipv_type_data=url_hosts_ipv_type
                    )
                    method_label = "订阅源" if method == "subscribe" else method
                    print(f"{method_label}:", len(name_results), end=", ")
            print_channel_number(data, cate, name)

    if config.open_unmatch_category and subscribe_result:
        unmatch_result = {
            name: info_list
            for name, info_list in subscribe_result.items()
            if name not in source_names
        }
        if unmatch_result:
            for name, info_list in unmatch_result.items():
                append_data_to_info_data(
                    data,
                    unmatch_category,
                    name,
                    info_list,
                    origin="subscribe",
                    whitelist_maps=whitelist_maps,
                    blacklist=blacklist,
                    ipv_type_data=url_hosts_ipv_type,
                    skip_validation=True,
                )


def is_valid_speed_result(info) -> bool:
    """
    Check if the speed test result is valid
    """
    try:
        delay = info.get("delay")
        if delay is None or delay == -1:
            return False

        res_str = info.get("resolution") or ""
        speed_val = info.get("speed", 0) or 0
        if not speed_val or math.isinf(speed_val):
            return False
        if open_filter_speed:
            if speed_val < resolution_speed_map.get(res_str, min_speed):
                return False

        if open_filter_resolution:
            try:
                res_value = get_resolution_value(res_str)
            except Exception:
                res_value = 0
            if res_value < min_resolution_value:
                return False

        return True
    except Exception:
        return False


async def test_speed(data, ipv6=False, callback=None, on_task_complete=None):
    """
    Test speed of channel data
    """
    ipv6_proxy_url = None if (not config.open_ipv6 or ipv6) else constants.ipv6_proxy
    open_full_speed_test = config.open_full_speed_test
    get_resolution = config.open_filter_resolution and check_ffmpeg_installed_status()
    performance = config.performance_settings
    concurrency = performance.speed_test_concurrency
    http_semaphore = asyncio.Semaphore(concurrency)
    probe_semaphore = asyncio.Semaphore(performance.probe_concurrency)
    speed_log_handler = get_logger(constants.speed_test_log_path, level=INFO, init=True)
    result_log_handler = get_logger(constants.result_log_path, level=INFO, init=True)
    logger = _LimitedLogger(speed_log_handler, 10000)
    result_logger = _LimitedLogger(result_log_handler, 10000)

    total_tasks = sum(len(info_list) for channel_obj in data.values() for info_list in channel_obj.values())
    total_tasks_by_channel = defaultdict(int)
    for cate, channel_obj in data.items():
        for name, info_list in channel_obj.items():
            total_tasks_by_channel[(cate, name)] += len(info_list)
    completed = 0
    grouped_results = {}
    completed_by_channel = defaultdict(int)
    urls_limit = config.urls_limit
    valid_count_by_channel = defaultdict(int)
    stopped_channels = set()

    def handle_result(cate, name, info, result):
        nonlocal completed
        if cate not in grouped_results:
            grouped_results[cate] = {}
        if name not in grouped_results[cate]:
            grouped_results[cate][name] = []
        merged = {**info, **result}
        grouped_results[cate][name].append(merged)

        if check_channel_need_frozen(merged):
            mark_url_bad(merged.get("url"))
        else:
            mark_url_good(merged.get("url"))

        is_valid = is_valid_speed_result(merged)
        reached_limit = False
        if is_valid:
            valid_count_by_channel[(cate, name)] += 1
            if not open_full_speed_test and valid_count_by_channel[(cate, name)] >= urls_limit:
                stopped_channels.add((cate, name))
                reached_limit = valid_count_by_channel[(cate, name)] == urls_limit

            try:
                origin = merged.get('origin')
                origin_name = "订阅源" if origin == "subscribe" else ("本地源" if origin == "local" else ("白名单" if origin == "whitelist" else origin))
                result_logger.info(
                    f"ID: {merged.get('id')}, 名称: {name}, "
                    f"接口: {merged.get('url')}, 来源: {origin_name}, "
                    f"IP类型: {merged.get('ipv_type')}, 地区: {merged.get('location')}, "
                    f"运营商: {merged.get('isp')}, "
                    f"延迟: {merged.get('delay') or -1} ms, 速度: {(merged.get('speed') or 0):.2f} M/s, "
                    f"分辨率: {merged.get('resolution')}, 帧率: {merged.get('fps') or '未知'}, "
                    f"视频编码: {merged.get('video_codec') or '未知'}, "
                    f"音频编码: {merged.get('audio_codec') or '未知'}"
                )
            except Exception:
                pass

        completed += 1
        completed_by_channel[(cate, name)] += 1

        is_channel_last = reached_limit or completed_by_channel[(cate, name)] >= total_tasks_by_channel.get((cate, name), 0)
        is_last = completed >= total_tasks

        if on_task_complete:
            try:
                on_task_complete(cate, name, merged, is_channel_last, is_last, is_valid)
            except Exception:
                pass

        if callback:
            try:
                callback()
            except Exception:
                pass

    def iter_items():
        for cate, channel_obj in data.items():
            for name, info_list in channel_obj.items():
                for info in info_list:
                    info['name'] = name
                    yield cate, name, info

    item_iterator = iter(iter_items())
    skipped = 0

    async with create_speed_test_session(concurrency) as session:
        async def worker():
            nonlocal skipped
            while True:
                try:
                    cate, name, info = next(item_iterator)
                except StopIteration:
                    return

                if (cate, name) in stopped_channels:
                    skipped += 1
                    continue
                result = {}
                try:
                    async with asyncio.timeout(config.speed_test_timeout):
                        result = await get_speed(
                            info,
                            headers=info.get("headers") or None,
                            ipv6_proxy=ipv6_proxy_url,
                            filter_resolution=get_resolution,
                            timeout=config.speed_test_timeout,
                            logger=logger,
                            session=session,
                            http_semaphore=http_semaphore,
                            probe_semaphore=probe_semaphore,
                        )
                except TimeoutError:
                    result = {}
                except Exception:
                    result = {}
                handle_result(cate, name, info, result)

        workers = [
            asyncio.create_task(worker())
            for _ in range(min(concurrency, total_tasks))
        ]
        if workers:
            await asyncio.gather(*workers)

    if skipped and callback:
        callback(skipped)

    close_logger_handlers(speed_log_handler)
    close_logger_handlers(result_log_handler)
    return grouped_results


def sort_channel_result(channel_data, result=None, filter_host=False, ipv6_support=True, cate=None, name=None):
    """
    Sort channel result
    """
    channel_result = defaultdict(lambda: defaultdict(list))
    categories = [cate] if cate else list(channel_data.keys())
    retain = retain_origin
    speed_lookup = get_speed_result
    sorter = get_sort_result
    unmatch_category = "♻️未匹配频道"

    for c in categories:
        obj = channel_data.get(c, {}) or {}
        names = [name] if name else list(obj.keys())
        for n in names:
            values = obj.get(n) or []
            whitelist_result = []
            result_list = (result.get(c, {}).get(n, []) if result else [])

            if c == unmatch_category:
                seen_urls = set()
                for item in values:
                    url = item.get("url")
                    if url and url not in seen_urls:
                        channel_result[c][n].append(item)
                        seen_urls.add(url)
                continue

            if filter_host:
                merged_items = []
                for value in values:
                    origin = value.get("origin")
                    if origin in retain or (not ipv6_support and result and value.get("ipv_type") == "ipv6"):
                        whitelist_result.append(value)
                    else:
                        host = value.get("host")
                        merged = {**value, **(speed_lookup(host) or {})}
                        merged_items.append(merged)

                sorter_input = chain(result_list, merged_items) if merged_items else result_list
                total_result = whitelist_result + sorter(sorter_input, ipv6_support=ipv6_support)
            else:
                for value in values:
                    origin = value.get("origin")
                    if origin in retain or (not ipv6_support and result and value.get("ipv_type") == "ipv6"):
                        whitelist_result.append(value)

                total_result = whitelist_result + sorter(result_list, ipv6_support=ipv6_support)

            seen_urls = set()
            for item in total_result:
                url = item.get("url")
                if url and url not in seen_urls:
                    channel_result[c][n].append(item)
                    seen_urls.add(url)

    return channel_result


def generate_channel_statistic(logger, cate, name, values):
    """
    Generate channel statistic
    """
    total = len(values)
    valid_items = [
        v for v in values
        if is_valid_speed_result(v)
    ]
    valid = len(valid_items)
    valid_rate = (valid / total * 100) if total > 0 else 0
    ipv4_count = len([v for v in values if v.get("ipv_type") == "ipv4"])
    ipv6_count = len([v for v in values if v.get("ipv_type") == "ipv6"])
    min_delay = min((v.get("delay") for v in values if (v.get("delay") or -1) != -1), default=-1)
    max_speed = max(
        (v.get("speed") for v in values if (v.get("speed") or 0) > 0 and not math.isinf(v.get("speed"))),
        default=0
    )
    avg_speed = sum((v.get("speed") or 0) for v in valid_items) / valid if valid > 0 else 0
    max_resolution = max(
        (v.get("resolution") for v in values if v.get("resolution")),
        key=lambda r: get_resolution_value(r),
        default="None"
    )
    video_codecs = [v.get('video_codec') for v in values if v.get('video_codec')]
    audio_codecs = [v.get('audio_codec') for v in values if v.get('audio_codec')]
    fps_values = [float(v.get('fps')) for v in values if
                  v.get('fps') is not None and isinstance(v.get('fps'), (int, float, str)) and str(
                      v.get('fps')).replace('.', '').isdigit()]
    most_video = Counter(video_codecs).most_common(1)
    most_audio = Counter(audio_codecs).most_common(1)
    most_video_str = most_video[0][0] if most_video else '未知'
    most_audio_str = most_audio[0][0] if most_audio else '未知'
    avg_fps = (sum(fps_values) / len(fps_values)) if fps_values else None
    if config.open_full_speed_test:
        content = f"分类: {cate}, 名称: {name}, 总计: {total}, 有效: {valid}, 有效率: {valid_rate:.2f}%, IPv4: {ipv4_count}, IPv6: {ipv6_count}, 最小延迟: {min_delay} ms, 最大速度: {max_speed:.2f} M/s, 平均速度: {avg_speed:.2f} M/s, 最大分辨率: {max_resolution}, 平均帧率: {f'{avg_fps:.2f}' if avg_fps is not None else '未知'}, 视频编码: {most_video_str}, 音频编码: {most_audio_str}"
        logger.info(content)
        print(f"📊 {content}")
    else:
        content = f"分类: {cate}, 名称: {name}, 有效: {valid}, IPv4: {ipv4_count}, IPv6: {ipv6_count}, 最小延迟: {min_delay} ms, 最大速度: {max_speed:.2f} M/s, 平均速度: {avg_speed:.2f} M/s, 最大分辨率: {max_resolution}, 平均帧率: {f'{avg_fps:.2f}' if avg_fps is not None else '未知'}, 视频编码: {most_video_str}, 音频编码: {most_audio_str}"
        logger.info(content)
        print(f"📊 {content}")


_WRITTEN_CONTENT_DIGESTS = {}


def process_write_content(
        path: str,
        data: CategoryChannelData,
        hls_url: str = None,
        open_empty_category: bool = False,
        ipv_type_prefer: list[str] = None,
        origin_type_prefer: list[str] = None,
        first_channel_name: str = None,
        enable_log: bool = False,
        is_last: bool = False,
):
    """
    Get channel write content
    :param path: write into path
    :param data: channel data
    :param hls_url: hls url
    :param open_empty_category: show empty category
    :param ipv_type_prefer: ipv type prefer
    :param origin_type_prefer: origin type prefer
    :param first_channel_name: the first channel name
    :param enable_log: enable log
    :param is_last: is last write
    """
    content = ""
    no_result_name = []
    first_cate = True
    result_data = defaultdict(list)
    custom_print.disable = not enable_log
    rtmp_type = ["hls"] if hls_url else []
    open_url_info = config.open_url_info
    unmatch_category = "♻️未匹配频道"
    seen_channel_names = set()

    for cate, channel_obj in data.items():
        cate_content = ""
        category_has_channels = False
        channel_obj_keys = channel_obj.keys()
        for i, name in enumerate(channel_obj_keys):
            formatted_name = format_channel_name(name)
            if name in seen_channel_names or (formatted_name and formatted_name in seen_channel_names):
                continue

            info_list = data.get(cate, {}).get(name, [])
            channel_urls = _get_total_urls_cached(
                info_list,
                ipv_type_prefer,
                origin_type_prefer,
                rtmp_type,
                apply_limit=cate != unmatch_category,
            )
            result_data[name].extend(channel_urls)
            if not channel_urls:
                if open_empty_category and name not in no_result_name:
                    no_result_name.append(name)
                continue

            seen_channel_names.add(name)
            if formatted_name:
                seen_channel_names.add(formatted_name)

            category_has_channels = True
            for item in channel_urls:
                item_url = item["url"]
                extra_info = item.get("extra_info", "")
                if open_url_info and extra_info:
                    item_url = add_url_info(item_url, extra_info)
                total_item_url = f"{hls_url}/{item['id']}.m3u8" if hls_url else item_url
                cate_content += f"\n{name},{total_item_url}"

        if category_has_channels:
            content += f"{'\n\n' if not first_cate else ''}{cate},#genre#{cate_content}"
            first_cate = False

    if open_empty_category and no_result_name and is_last:
        filtered_no_result = [
            n for n in no_result_name
            if n not in seen_channel_names and format_channel_name(n) not in seen_channel_names
        ]
        if filtered_no_result:
            custom_print("\n🈚 无结果频道名称：")
            content += f"{'\n\n' if not first_cate else ''}🈚无结果频道,#genre#"
            for i, name in enumerate(filtered_no_result):
                end_char = ", " if i < len(filtered_no_result) - 1 else ""
                custom_print(name, end=end_char)
                content += f"\n{name},url"

    render_hasher = hashlib.sha256(content.encode("utf-8"))
    render_hasher.update(
        repr((
            is_last,
            first_channel_name,
            config.open_epg,
            config.open_update_time,
            config.update_time_position,
            config.logo_url,
            config.logo_type,
            config.open_subscribe_logo,
            config.user_agent,
            config.cdn_url,
            get_public_url(),
        )).encode("utf-8")
    )
    render_signature = render_hasher.digest()
    m3u_path = os.path.splitext(path)[0] + ".m3u"
    if _WRITTEN_CONTENT_DIGESTS.get(path) == render_signature and os.path.exists(path) and os.path.exists(m3u_path):
        return False

    if config.open_update_time:
        update_time_item = next(
            (urls[0] for channel_obj in data.values()
             for info_list in channel_obj.values()
             if (urls := _get_total_urls_cached(
                info_list,
                ipv_type_prefer,
                origin_type_prefer,
                rtmp_type,
                apply_limit=True,
            ))),
            {"id": "id", "url": "url", "extra_info": ""}
        )
        now = get_datetime_now()
        update_time_item_url = update_time_item["url"]
        update_title = "⏰更新时间" if is_last else "⏰正在更新中，刷新获取最新结果"
        update_time_extra_info = update_time_item.get("extra_info", "")
        if open_url_info and update_time_extra_info:
            update_time_item_url = add_url_info(update_time_item_url, update_time_extra_info)
        value = f"{hls_url}/{update_time_item['id']}.m3u8" if hls_url else update_time_item_url
        if config.update_time_position == "top":
            content = f"{update_title},#genre#\n{now},{value}\n\n{content}"
        else:
            content += f"\n\n{update_title},#genre#\n{now},{value}"

    try:
        target_dir = os.path.dirname(path) or "."
        os.makedirs(target_dir, exist_ok=True)

        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, dir=target_dir,
                                          prefix=os.path.basename(path) + ".tmp.") as tmpf:
            tmpf.write(content)
            tmp_path = tmpf.name
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o644)
        except Exception:
            pass

        if config.open_m3u_result:
            from utils.tools import convert_to_m3u
            convert_to_m3u(path=path, first_channel_name=first_channel_name, data=result_data, content=content)

        json_data = convert_to_json_v1(content=content)
        json_path = os.path.splitext(path)[0] + ".json"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, dir=target_dir,
                                          prefix=os.path.basename(json_path) + ".tmp.") as tmpf:
            import json
            json.dump(json_data, tmpf, ensure_ascii=False, indent=4)
            tmp_path = tmpf.name
        os.replace(tmp_path, json_path)
        try:
            os.chmod(json_path, 0o644)
        except Exception:
            pass

        _WRITTEN_CONTENT_DIGESTS[path] = render_signature
        if config.open_pg:
            from utils.pg_db import save_to_postgresql
            save_to_postgresql(json_data)
    except Exception as e:
        print(f"❌ 写入结果文件出错：convert json error: {e}", flush=True)
        return False
    return True


def write_channel_to_file(data, ipv6=False, first_channel_name=None, skip_print=False, is_last=False):
    """
    Write channel to file
    """
    try:
        if not skip_print:
            print("正在写入结果，生成结果文件...", flush=True)
        open_empty_category = config.open_empty_category
        ipv_type_prefer = list(config.ipv_type_prefer)
        if any(pref == "auto" for pref in ipv_type_prefer):
            ipv_type_prefer = ["ipv6", "ipv4"] if ipv6 else ["ipv4", "ipv6"]
        origin_type_prefer = config.origin_type_prefer
        file_list = [
            {"path": config.final_file, "enable_log": True}
        ]

        for file in file_list:
            process_write_content(
                path=file["path"],
                data=data,
                hls_url=None,
                open_empty_category=open_empty_category,
                ipv_type_prefer=ipv_type_prefer,
                origin_type_prefer=origin_type_prefer,
                first_channel_name=first_channel_name,
                enable_log=file.get("enable_log", False),
                is_last=is_last
            )
        if not skip_print:
            print("✅ 结果文件生成成功", flush=True)
    except Exception as e:
        print(f"❌ 写入结果文件出错：{e}", flush=True)
