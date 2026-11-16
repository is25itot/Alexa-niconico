import os
import logging
import boto3

from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.dispatch_components import (AbstractRequestHandler, AbstractExceptionHandler)
from ask_sdk_core.utils import is_request_type, is_intent_name
from ask_sdk_model.interfaces.audioplayer import (PlayDirective, PlayBehavior, AudioItem, Stream, StopDirective)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 再生する曲のリスト(S3の Media/ に置いたファイル)
PLAYLIST = [
    "Media/test.mp3",
    "Media/test2.mp3",
    "Media/test3.mp3",
]


def create_long_presigned_url(object_name, expires=3600):
    """S3の署名付きURLを作る。予約した曲の再生が少し先でも切れないよう1時間有効にする"""
    s3_client = boto3.client(
        "s3",
        region_name=os.environ.get("S3_PERSISTENCE_REGION"),
        config=boto3.session.Config(
            signature_version="s3v4", s3={"addressing_style": "path"}))
    return s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": os.environ.get("S3_PERSISTENCE_BUCKET"),
                "Key": object_name},
        ExpiresIn=expires)


def make_token(index):
    return "track-{}".format(index)


def parse_index(token):
    """トークン("track-1" など)から曲の番号を取り出す。読めなければ0"""
    try:
        return int(token.split("-")[1]) % len(PLAYLIST)
    except (AttributeError, IndexError, ValueError):
        return 0


def build_play_directive(index, behavior, previous_token=None, offset=0):
    return PlayDirective(
        play_behavior=behavior,
        audio_item=AudioItem(stream=Stream(
            token=make_token(index),
            url=create_long_presigned_url(PLAYLIST[index]),
            offset_in_milliseconds=offset,
            expected_previous_token=previous_token)))


def play_track(handler_input, speech, index=0, offset=0):
    builder = handler_input.response_builder
    if speech:
        builder.speak(speech)
    return (
        builder
        .add_directive(build_play_directive(
            index, PlayBehavior.REPLACE_ALL, offset=offset))
        .set_should_end_session(True)
        .response
    )


class LaunchRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        return play_track(handler_input, "再生します。")


class PlayMusicIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("PlayMusicIntent")(handler_input)

    def handle(self, handler_input):
        return play_track(handler_input, "再生します。")


class ResumeIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.ResumeIntent")(handler_input)

    def handle(self, handler_input):
        audio = handler_input.request_envelope.context.audio_player
        offset = (audio.offset_in_milliseconds or 0) if audio else 0
        return play_track(handler_input, "再開します。", current_index(handler_input), offset)


def current_index(handler_input):
    """今再生している(していた)曲の番号。分からなければ0"""
    audio = handler_input.request_envelope.context.audio_player
    return parse_index(audio.token) if audio and audio.token else 0


class NextIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.NextIntent")(handler_input)

    def handle(self, handler_input):
        next_index = (current_index(handler_input) + 1) % len(PLAYLIST)
        return play_track(handler_input, None, next_index)


class PreviousIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.PreviousIntent")(handler_input)

    def handle(self, handler_input):
        prev_index = (current_index(handler_input) - 1) % len(PLAYLIST)
        return play_track(handler_input, None, prev_index)


class StopHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return any(is_intent_name(n)(handler_input) for n in (
            "AMAZON.PauseIntent", "AMAZON.StopIntent", "AMAZON.CancelIntent"))

    def handle(self, handler_input):
        return (
            handler_input.response_builder
            .add_directive(StopDirective())
            .set_should_end_session(True)
            .response
        )


class NearlyFinishedHandler(AbstractRequestHandler):
    """再生中の曲がそろそろ終わるとき、次の曲を予約する(最後の曲のあとは1曲目に戻る)"""
    def can_handle(self, handler_input):
        return is_request_type("AudioPlayer.PlaybackNearlyFinished")(handler_input)

    def handle(self, handler_input):
        current_token = handler_input.request_envelope.request.token
        next_index = (parse_index(current_token) + 1) % len(PLAYLIST)
        logger.info("NearlyFinished: %s -> track-%s", current_token, next_index)
        return (
            handler_input.response_builder
            .add_directive(build_play_directive(
                next_index, PlayBehavior.ENQUEUE,
                previous_token=current_token))
            .response
        )


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
                .speak("すみません、うまくいきませんでした。")
                .response)


sb = SkillBuilder()
sb.add_request_handler(LaunchRequestHandler())
sb.add_request_handler(PlayMusicIntentHandler())
sb.add_request_handler(ResumeIntentHandler())
sb.add_request_handler(StopHandler())
sb.add_request_handler(NearlyFinishedHandler())   # 必ず AudioPlayerEventHandler より前に登録
sb.add_request_handler(AudioPlayerEventHandler())
sb.add_request_handler(SessionEndedRequestHandler())
sb.add_exception_handler(CatchAllExceptionHandler())

lambda_handler = sb.lambda_handler()
