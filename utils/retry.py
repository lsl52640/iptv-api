from time import sleep

max_retries = 2


def retry_func(func, retries=max_retries, name=""):
    """
    Retry the function
    """
    for i in range(retries):
        try:
            sleep(1)
            return func()
        except Exception as e:
            if name and i < retries - 1:
                print(f"🔄 请求{name}失败，正在进行第{i + 1}次重试...", flush=True)
            elif i == retries - 1:
                raise Exception(
                    f"❌ 请求{name}失败，已达到最大重试次数"
                )
    raise Exception(f"❌ 请求{name}失败，已达到最大重试次数")
