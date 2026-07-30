"""KIS "1분당 1회" 토큰 재발급 제한 대응.

`get_token()`의 설계는 이랬다:

    5. KIS API 실패 ("1분 내 재발급" 등) → 만료 안 된 캐시 폴백

그런데 그 폴백은 **run 간에 캐시가 남아 있다**는 전제 위에 있었다. GitHub
Actions에선 `download-artifact@v4`가 artifact를 run 단위로 스코프해서 이전 run의
캐시를 못 본다 — 실제 운영 로그에서 매 run 이렇게 찍혔다:

    ##[error]Unable to download artifact(s): Artifact not found for name: kis-token-cache

즉 5번 폴백은 실환경에서 **도달 불가능한 죽은 코드**였고, 제한에 걸리면 폴백할
캐시가 없어 `RuntimeError`가 그대로 올라와 인증이 통째로 죽었다.

artifact로 되살리는 길은 막혀 있다 — 이 레포는 public이라 artifact가 곧 공개
배포이고, 거기 담기는 건 주문 권한이 붙은 24시간짜리 액세스 토큰이다. 그래서
캐시를 공유하는 대신 **제한이 풀릴 때까지 기다렸다 재시도**한다.
"""

import pytest

import src.kis_auth as auth


class FakeResp:
    def __init__(self, status_code, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload


THROTTLED = FakeResp(
    403, '{"error_code":"EGW00133","error_description":"접근토큰 발급 잠시 후 다시 시도하세요(1분당 1회)"}')
OK = FakeResp(200, payload={"access_token": "tok-abc", "expires_in": 86400})


@pytest.fixture
def no_sleep(monkeypatch):
    """대기를 없애고 호출된 초를 기록한다 (테스트가 62초씩 자면 안 된다)."""
    slept = []
    monkeypatch.setattr(auth.time, "sleep", lambda s: slept.append(s))
    # settings는 pydantic 모델이라 인스턴스 속성을 못 갈아끼운다 → 클래스 쪽을 패치
    monkeypatch.setattr(type(auth.settings), "validate_runtime",
                        lambda self: None, raising=False)
    return slept


def _post_returning(monkeypatch, responses):
    calls = []

    def _post(url, json=None, headers=None, timeout=None):
        calls.append(url)
        r = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(auth.requests, "post", _post)
    return calls


# ──────────────────────────────────────────────────────────
# 제한 감지
# ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    '{"error_code":"EGW00133","error_description":"..."}',
    "접근토큰 발급은 1분당 1회로 제한됩니다",
    "잠시 후 다시 시도해 주세요",
    "Please try again later",
])
def test_throttle_detected(text):
    """KIS가 제한을 알리는 방식이 환경마다 달라 여러 표지를 본다."""
    assert auth._is_reissue_throttled(FakeResp(403, text)) is True


@pytest.mark.parametrize("resp", [
    FakeResp(401, '{"error_description":"appkey가 유효하지 않습니다"}'),
    FakeResp(500, "internal server error"),
    None,
])
def test_non_throttle_errors_not_misread(resp):
    """진짜 인증 실패를 제한으로 오인하면 무의미하게 62초씩 기다린다."""
    assert auth._is_reissue_throttled(resp) is False


def test_success_is_not_throttle():
    assert auth._is_reissue_throttled(FakeResp(200, "ok")) is False


# ──────────────────────────────────────────────────────────
# 재시도 동작
# ──────────────────────────────────────────────────────────

def test_throttled_then_succeeds(monkeypatch, no_sleep):
    """제한 → 대기 → 재발급 성공. 예전엔 여기서 인증이 죽었다."""
    calls = _post_returning(monkeypatch, [THROTTLED, OK])

    bundle = auth._request_new_token()

    assert bundle.access_token == "tok-abc"
    assert len(calls) == 2, "재시도하지 않았다"
    assert auth.REISSUE_RETRY_WAIT_SEC in no_sleep, "제한 해제까지 기다리지 않았다"


def test_throttle_wait_is_long_enough(no_sleep):
    """KIS 제한이 1분이므로 그보다 짧게 기다리면 또 걸린다."""
    assert auth.REISSUE_RETRY_WAIT_SEC > 60


def test_persistent_throttle_eventually_raises(monkeypatch, no_sleep):
    """무한 재시도는 안 된다 — 시도를 소진하면 실패로 올린다."""
    calls = _post_returning(monkeypatch, [THROTTLED])

    with pytest.raises(RuntimeError, match="토큰 발급 실패"):
        auth._request_new_token()

    assert len(calls) == auth.TOKEN_REQUEST_ATTEMPTS


def test_auth_failure_does_not_wait(monkeypatch, no_sleep):
    """앱키가 틀린 건 기다린다고 풀리지 않는다 — 즉시 실패해야 한다."""
    bad = FakeResp(401, '{"error_description":"appkey가 유효하지 않습니다"}')
    calls = _post_returning(monkeypatch, [bad])

    with pytest.raises(RuntimeError):
        auth._request_new_token()

    assert len(calls) == 1, "재시도할 이유가 없는 오류로 재시도했다"
    assert no_sleep == [], "인증 오류인데 대기했다"


def test_network_error_still_retried(monkeypatch, no_sleep):
    """기존 타임아웃 재시도가 살아 있어야 한다 (회귀 방지)."""
    import requests as rq
    calls = _post_returning(monkeypatch, [rq.exceptions.ConnectionError("boom"), OK])

    bundle = auth._request_new_token()

    assert bundle.access_token == "tok-abc"
    assert len(calls) == 2
    assert auth.NETWORK_RETRY_WAIT_SEC in no_sleep


def test_network_error_exhausted_raises(monkeypatch, no_sleep):
    import requests as rq
    _post_returning(monkeypatch, [rq.exceptions.Timeout("t")])
    with pytest.raises(rq.exceptions.Timeout):
        auth._request_new_token()


def test_immediate_success_makes_one_call(monkeypatch, no_sleep):
    calls = _post_returning(monkeypatch, [OK])
    auth._request_new_token()
    assert len(calls) == 1 and no_sleep == []
