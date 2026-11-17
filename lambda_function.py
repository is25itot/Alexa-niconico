import os
import json
import random
import uuid
import hashlib
import logging
import boto3
import unicodedata
import datetime

from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.dispatch_components import (AbstractRequestHandler, AbstractExceptionHandler, AbstractRequestInterceptor)
from ask_sdk_core.utils import is_request_type, is_intent_name
from ask_sdk_model.interfaces.audioplayer import (PlayDirective, PlayBehavior, AudioItem, Stream, StopDirective)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------
# 曲リスト(idは英数字のみ。"." は使わない。対話モデルのスロット値のidと一致させる)
# ---------------------------------------------------------------
LIBRARY = [
    {"id": "test1",  "key": "Media/test1.mp3",  "title": "テスト1","aliases": ["テストいち", "テスト一"]},
    {"id": "test2", "key": "Media/test2.mp3", "title": "テスト2","aliases": ["テストに", "テスト二"]},
    {"id": "test3", "key": "Media/test3.mp3", "title": "テスト3","aliases": ["テストさん", "テスト三"]},
    {"id": "test4", "key": "Media/test4.mp3", "title": "テスト4","aliases": ["テストよん", "テスト四"]},
    {"id": "test5", "key": "Media/test5.mp3", "title": "テスト5","aliases": ["テストご", "テスト五"]},
    {"id": "test6", "key": "Media/test6.mp3", "title": "テスト6","aliases": ["テストろく", "テスト六"]},
]
SONGS = {s["id"]: s for s in LIBRARY}
SONG_IDS = [s["id"] for s in LIBRARY]

# ---------------------------------------------------------------
# S3(署名付きURLと、再生設定の保存)
# ---------------------------------------------------------------
BUCKET = os.environ.get("S3_PERSISTENCE_BUCKET")
s3_client = boto3.client(
    "s3",
    region_name=os.environ.get("S3_PERSISTENCE_REGION"),
    config=boto3.session.Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        connect_timeout=2,
        read_timeout=3,
        retries={"max_attempts": 1}))


def create_long_presigned_url(object_name, expires=3600):
    return s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": object_name},
        ExpiresIn=expires)


def state_key(handler_input):
    user_id = handler_input.request_envelope.context.system.user.user_id
    return "State/{}.json".format(hashlib.sha256(user_id.encode()).hexdigest()[:32])


def make_order(shuffle, first=None):
    """再生順(曲idのリスト)を作る。シャッフル時は first を先頭にできる"""
    order = list(SONG_IDS)
    if shuffle:
        random.shuffle(order)
        if first in order:
            order.remove(first)
            order.insert(0, first)
    return order


def load_state(handler_input):
    try:
        obj = s3_client.get_object(Bucket=BUCKET, Key=state_key(handler_input))
        state = json.loads(obj["Body"].read())
    except Exception as e:
        logger.info("状態を読み込めませんでした(初回の可能性): %s", e)
        state = {}
    state.setdefault("shuffle", False)
    state.setdefault("loop", "all")   # "off" | "all" | "one"
    # 曲リストを変更した場合などは、再生順を作り直す
    if sorted(state.get("order", [])) != sorted(SONG_IDS):
        state["order"] = make_order(state["shuffle"])
    return state


def save_state(handler_input, state):
    try:
        s3_client.put_object(
            Bucket=BUCKET, Key=state_key(handler_input),
            Body=json.dumps(state).encode("utf-8"))
    except Exception as e:
        logger.error("状態を保存できませんでした: %s", e)


# ---------------------------------------------------------------
# 曲の選び方
# ---------------------------------------------------------------
def next_song(state, current, manual):
    """次に再生する曲のidを返す。再生を終えるべきときは None。
    manual=True は「次の曲」と言われた場合(最後の曲でも先頭へ戻る)"""
    if state["loop"] == "one" and not manual and current in SONGS:
        return current
    order = state["order"]
    pos = order.index(current) if current in order else -1
    if pos + 1 < len(order):
        return order[pos + 1]
    # 最後の曲まで来た
    if state["loop"] == "off" and not manual:
        return None
    if state["shuffle"]:  # 一周したら並べ直す(直前の曲が先頭に来ないようにする)
        new_order = make_order(True)
        if len(new_order) > 1 and new_order[0] == current:
            new_order[0], new_order[1] = new_order[1], new_order[0]
        state["order"] = new_order
    return state["order"][0]


def prev_song(state, current):
    order = state["order"]
    pos = order.index(current) if current in order else 0
    return order[(pos - 1) % len(order)]


# ---------------------------------------------------------------
# トークンと再生指示
# ---------------------------------------------------------------
def make_token(song_id):
    # 同じ曲を続けて流しても区別できるよう、ランダムな文字列を付ける
    return "{}.{}".format(song_id, uuid.uuid4().hex[:8])


def parse_song_id(token):
    if not token:
        return None
    song_id = token.split(".")[0]
    return song_id if song_id in SONGS else None


def current_song_id(handler_input):
    audio = handler_input.request_envelope.context.audio_player
    return parse_song_id(audio.token) if audio and audio.token else None


def build_play_directive(song_id, behavior, previous_token=None, offset=0):
    return PlayDirective(
        play_behavior=behavior,
        audio_item=AudioItem(stream=Stream(
            token=make_token(song_id),
            url=create_long_presigned_url(SONGS[song_id]["key"]),
            offset_in_milliseconds=offset,
            expected_previous_token=previous_token)))


def play_track(handler_input, speech, song_id, offset=0):
    builder = handler_input.response_builder
    if speech:
        builder.speak(speech)
    return (
        builder
        .add_directive(build_play_directive(
            song_id, PlayBehavior.REPLACE_ALL, offset=offset))
        .set_should_end_session(True)
        .response
    )


def normalize(text):
    """スペースを取り除き、全角・半角をそろえる(「テスト 2」→「テスト2」)"""
    return "".join(unicodedata.normalize("NFKC", text or "").split()).lower()


def resolve_song_id(handler_input):
    """スロット(曲名)から曲idを取り出す。見つからなければ None"""
    slots = handler_input.request_envelope.request.intent.slots or {}
    slot = slots.get("songTitle")
    spoken = normalize(slot.value) if slot else ""
    logger.info("songTitle: %r -> %r", slot.value if slot else None, spoken)

    # 1) 聞き取った言葉が、曲名または別名と一致する曲を探す
    if spoken:
        for song in LIBRARY:
            names = [song["title"]] + song.get("aliases", [])
            if spoken in [normalize(n) for n in names]:
                return song["id"]

    # 2) 一致しなければ、Alexaの解決結果が1件だけのときに採用する
    try:
        for auth in slot.resolutions.resolutions_per_authority:
            code = getattr(auth.status.code, "value", auth.status.code)
            if code == "ER_SUCCESS_MATCH" and len(auth.values) == 1:
                return auth.values[0].value.id
    except AttributeError:
        pass
    return None


# ---------------------------------------------------------------
# ハンドラ
# ---------------------------------------------------------------
class StartPlaybackHandler(AbstractRequestHandler):
    """スキル起動時、「音楽をかけて」"""
    def can_handle(self, handler_input):
        return (is_request_type("LaunchRequest")(handler_input)
                or is_intent_name("PlayMusicIntent")(handler_input))

    def handle(self, handler_input):
        intent_name = handler_input.request_envelope.request.intent.name
        key, value, speech = self.SETTINGS[intent_name]
        state = load_state(handler_input)
        state[key] = value
        song_id = current_song_id(handler_input)
        if key == "shuffle":  # 今の曲を先頭にして、順番を作り直す
            state["order"] = make_order(value, first=song_id)
        save_state(handler_input, state)

        builder = handler_input.response_builder.speak(speech)
        audio = handler_input.request_envelope.context.audio_player
        activity = getattr(audio.player_activity, "value", audio.player_activity) if audio else None
        if song_id and activity == "PLAYING":
            # 同じ曲を同じ位置から再開(予約済みの次の曲も破棄され、新設定で予約し直される)
            builder.add_directive(build_play_directive(song_id, PlayBehavior.REPLACE_ALL, offset=audio.offset_in_milliseconds or 0))
        return builder.set_should_end_session(True).response


class PlaySongIntentHandler(AbstractRequestHandler):
    """「〇〇をかけて」"""
    def can_handle(self, handler_input):
        return is_intent_name("PlaySongIntent")(handler_input)

    def handle(self, handler_input):
        song_id = resolve_song_id(handler_input)
        if song_id is None:
            return (handler_input.response_builder
                    .speak("その曲が見つかりませんでした。")
                    .set_should_end_session(True).response)
        state = load_state(handler_input)
        state["order"] = make_order(state["shuffle"], first=song_id)
        save_state(handler_input, state)
        return play_track(handler_input,
                          "{}を再生します。".format(SONGS[song_id]["title"]),
                          song_id)


class ResumeIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.ResumeIntent")(handler_input)

    def handle(self, handler_input):
        audio = handler_input.request_envelope.context.audio_player
        song_id = current_song_id(handler_input)
        if song_id is None:
            song_id = load_state(handler_input)["order"][0]
            offset = 0
        else:
            offset = (audio.offset_in_milliseconds or 0) if audio else 0
        return play_track(handler_input, "再開します。", song_id, offset)


class StopHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return any(is_intent_name(n)(handler_input) for n in (
            "AMAZON.PauseIntent", "AMAZON.StopIntent", "AMAZON.CancelIntent"))

    def handle(self, handler_input):
        return (handler_input.response_builder
                .add_directive(StopDirective())
                .set_should_end_session(True).response)


class NextIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.NextIntent")(handler_input)

    def handle(self, handler_input):
        state = load_state(handler_input)
        song_id = next_song(state, current_song_id(handler_input), manual=True)
        save_state(handler_input, state)
        return play_track(handler_input, None, song_id)


class PreviousIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.PreviousIntent")(handler_input)

    def handle(self, handler_input):
        state = load_state(handler_input)
        song_id = prev_song(state, current_song_id(handler_input))
        return play_track(handler_input, None, song_id)


class PlaybackModeHandler(AbstractRequestHandler):
    """シャッフル・ループの設定変更"""
    SETTINGS = {
        "AMAZON.ShuffleOnIntent":  ("shuffle", True,  "シャッフルをオンにしました。"),
        "AMAZON.ShuffleOffIntent": ("shuffle", False, "シャッフルをオフにしました。"),
        "AMAZON.LoopOnIntent":     ("loop", "all",    "全曲ループをオンにしました。"),
        "AMAZON.LoopOffIntent":    ("loop", "off",    "ループをオフにしました。"),
        "LoopOneIntent":           ("loop", "one",    "1曲ループをオンにしました。"),
    }

    def can_handle(self, handler_input):
        return any(is_intent_name(n)(handler_input) for n in self.SETTINGS)

    def handle(self, handler_input):
        intent_name = handler_input.request_envelope.request.intent.name
        key, value, speech = self.SETTINGS[intent_name]
        state = load_state(handler_input)
        state[key] = value
        if key == "shuffle":  # 今の曲を先頭にして、順番を作り直す
            state["order"] = make_order(value, first=current_song_id(handler_input))
        save_state(handler_input, state)
        return (handler_input.response_builder
                .speak(speech).set_should_end_session(True).response)


class NearlyFinishedHandler(AbstractRequestHandler):
    """曲がそろそろ終わるとき、次の曲を予約する"""
    def can_handle(self, handler_input):
        return is_request_type("AudioPlayer.PlaybackNearlyFinished")(handler_input)

    def handle(self, handler_input):
        current_token = handler_input.request_envelope.request.token
        state = load_state(handler_input)
        song_id = next_song(state, parse_song_id(current_token), manual=False)
        save_state(handler_input, state)
        if song_id is None:  # ループなしで最後の曲まで来た → 予約せず、そのまま終了
            return handler_input.response_builder.response
        logger.info("NearlyFinished: %s -> %s", current_token, song_id)
        return (handler_input.response_builder
                .add_directive(build_play_directive(
                    song_id, PlayBehavior.ENQUEUE, previous_token=current_token))
                .response)


class AudioPlayerEventHandler(AbstractRequestHandler):
    """その他のAudioPlayerイベントは、ログだけ残して受け流す"""
    def can_handle(self, handler_input):
        return handler_input.request_envelope.request.object_type.startswith(
            "AudioPlayer.")

    def handle(self, handler_input):
        request = handler_input.request_envelope.request
        if request.object_type == "AudioPlayer.PlaybackFailed":
            logger.error("PlaybackFailed: %s", request)
        else:
            logger.info("AudioPlayer event: %s", request.object_type)
        return handler_input.response_builder.response


class SessionEndedRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_request_type("SessionEndedRequest")(handler_input)

    def handle(self, handler_input):
        return handler_input.response_builder.response


class CatchAllExceptionHandler(AbstractExceptionHandler):
    def can_handle(self, handler_input, exception):
        return True

    def handle(self, handler_input, exception):
        logger.error(exception, exc_info=True)
        return (handler_input.response_builder
                .speak("すみません、うまくいきませんでした。").response)


def debug_log(message):
    """届いたリクエストをS3の Debug/events.json に残す(直近40件)"""
    key = "Debug/events.json"
    try:
        try:
            lines = json.loads(s3_client.get_object(Bucket=BUCKET, Key=key)["Body"].read())
        except Exception:
            lines = []
        lines.append("{} {}".format(
            datetime.datetime.utcnow().isoformat(timespec="seconds"), message))
        s3_client.put_object(
            Bucket=BUCKET, Key=key,
            Body=json.dumps(lines[-40:], ensure_ascii=False).encode("utf-8"))
    except Exception as e:
        logger.error("デバッグ記録に失敗: %s", e)


class DebugInterceptor(AbstractRequestInterceptor):
    def process(self, handler_input):
        r = handler_input.request_envelope.request
        parts = [r.object_type]
        intent = getattr(r, "intent", None)
        if intent:
            parts.append(intent.name)
        token = getattr(r, "token", None)
        if token:
            parts.append("token=" + token)
        err = getattr(r, "error", None)
        if err:
            parts.append("error=" + str(err))
        debug_log(" ".join(parts))


sb = SkillBuilder()
sb.add_global_request_interceptor(DebugInterceptor())
sb.add_request_handler(StartPlaybackHandler())
sb.add_request_handler(PlaySongIntentHandler())
sb.add_request_handler(ResumeIntentHandler())
sb.add_request_handler(StopHandler())
sb.add_request_handler(NextIntentHandler())
sb.add_request_handler(PreviousIntentHandler())
sb.add_request_handler(PlaybackModeHandler())
sb.add_request_handler(NearlyFinishedHandler())   # 必ず AudioPlayerEventHandler より前に登録
sb.add_request_handler(AudioPlayerEventHandler())
sb.add_request_handler(SessionEndedRequestHandler())
sb.add_exception_handler(CatchAllExceptionHandler())

lambda_handler = sb.lambda_handler()
