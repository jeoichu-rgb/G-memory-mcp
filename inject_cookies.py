from playwright.sync_api import sync_playwright
import os

cookies = [
    {"name": "acw_tc", "value": "0a00d41117771176354553360e7ad1d52df2a46e9f314c8af90d9f8d42793b", "domain": ".xiaohongshu.com", "path": "/"},
    {"name": "abRequestId", "value": "fa289469-c75b-5be3-bd99-19ca085442c6", "domain": ".xiaohongshu.com", "path": "/"},
    {"name": "a1", "value": "19dc4772625vqki0wryuapcqdp2k8atkbtonpn6wd50000406479", "domain": ".xiaohongshu.com", "path": "/"},
    {"name": "webId", "value": "b114a43a057b9d74c23b65796c91d67e", "domain": ".xiaohongshu.com", "path": "/"},
    {"name": "web_session", "value": "040069b5ff900519109dbfe1d93b4b6a748724", "domain": ".xiaohongshu.com", "path": "/"},
    {"name": "gid", "value": "yjfS4WWqW0DqyjfS4WWJK9IKJ2hAT38EFv70lEfUSAfUJI28TY09TU88848K4Wj82qYjJfjD", "domain": ".xiaohongshu.com", "path": "/"},
    {"name": "xsecappid", "value": "xhs-pc-web", "domain": ".xiaohongshu.com", "path": "/"},
]

os.makedirs("/app/browser_profile", exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(
        "/app/browser_profile",
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"]
    )
    page = browser.new_page()
    page.goto("https://www.xiaohongshu.com")
    browser.add_cookies(cookies)
    page.reload()
    print(page.title())
    browser.close()
    print("完成")
