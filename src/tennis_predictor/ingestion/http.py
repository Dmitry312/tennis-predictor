"""Общий HTTP-слой для всех коллекторов: единая сессия с retry/backoff,
per-source заголовки/куки в одном месте вместо дублирования по скриптам.
"""

import requests
from requests.adapters import HTTPAdapter, Retry

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/26.5 Safari/605.1.15"
)

# Снимает интерстишл-редирект на SberID — см. docs/decisions.md
# ("championat.com: обход SberID-редиректа через cookie unity_pause_sso").
CHAMPIONAT_COOKIES = {"unity_pause_sso": "1"}


def make_session(
    *, cookies: dict[str, str] | None = None, headers: dict[str, str] | None = None
) -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers["User-Agent"] = DEFAULT_USER_AGENT
    if headers:
        session.headers.update(headers)
    if cookies:
        session.cookies.update(cookies)
    return session
