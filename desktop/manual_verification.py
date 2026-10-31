"""Human-entered image verification using the paused account's HTTP session."""
import secrets
from urllib.parse import urlsplit, urljoin


class VerificationUnavailable(Exception):
    pass


class ManualVerifier:
    def __init__(self, session, request, origin, headers=None):
        self.session, self.request = session, request
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or parsed.hostname not in (
                "mooc1.chaoxing.com", "mooc2.chaoxing.com", "mooc1-1.chaoxing.com"):
            raise VerificationUnavailable("此验证页面暂不支持在程序中显示，请从课程入口进入原课程检查。")
        self.origin = "https://" + parsed.hostname
        # Match the blocked request's UA without copying credentials into headers.
        self.headers = {k: v for k, v in (headers or {}).items()
                        if k.lower() in ("user-agent", "referer")}
        self.loaded = False
        self.submissions = 0

    def fetch(self):
        self.loaded = False
        result = self.request("GET", self.origin + "/processVerifyPng.ac",
                              params={"t": secrets.randbelow(2147483647)},
                              headers=self.headers, timeout=15, allow_redirects=False, verify=True)
        if result.status_code != 200 or not result.headers.get("Content-Type", "").lower().startswith("image/") \
                or not result.content.startswith(b"\x89PNG\r\n\x1a\n") or len(result.content) > 3_000_000:
            raise VerificationUnavailable("平台没有返回图片验证码。可能是登录、权限或其他验证方式；请停止后重新登录，或从课程入口进入原课程检查。")
        self.loaded = True
        return result.content

    def submit(self, answer):
        answer = answer.strip()
        if not self.loaded or not answer or len(answer) > 32 or self.submissions >= 3:
            raise VerificationUnavailable("请先显示验证码并填写；本窗口最多提交 3 次。")
        self.loaded = False
        self.submissions += 1
        result = self.request("GET", self.origin + "/html/processVerify.ac",
                              params={"ucode": answer, "app": 0}, headers=self.headers,
                              timeout=15, allow_redirects=False, verify=True)
        location = urlsplit(urljoin(self.origin, result.headers.get("Location", "")))
        # A redirect is only an acknowledgement. The original chapter must still be checked.
        acknowledged = result.status_code in (302, 303) and bool(result.headers.get("Location")) \
            and location.scheme == "https" and location.hostname == urlsplit(self.origin).hostname \
            and not any(part in location.path.lower() for part in ("login", "verify"))
        if acknowledged:
            from api.cookies import save_cookies
            save_cookies(self.session)
        return acknowledged
