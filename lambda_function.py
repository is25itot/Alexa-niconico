import logging

from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.dispatch_components import (
    AbstractRequestHandler, AbstractExceptionHandler)
from ask_sdk_core.utils import is_request_type, is_intent_name
from ask_sdk_model.interfaces.audioplayer import (
    PlayDirective, PlayBehavior, AudioItem, Stream, StopDirective)

from utils import create_presigned_url  # テンプレートに入っているS3用の関数

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

AUDIO_KEY = "Media/audio.mp3"   # S3に置いたファイル
TRACK_TOKEN = "track-001"       # 曲を識別する文字列(後でキュー管理に使う)


def play_audio(handler_input, speech):
    url = create_presigned_url(AUDIO_KEY)
    return (
        handler_input.response_builder
        .speak(speech)
        .add_directive(PlayDirective(
            play_behavior=PlayBehavior.REPLACE_ALL,
            audio_item=AudioItem(stream=Stream(
                token=TRACK_TOKEN,
                url=url,
                offset_in_milliseconds=0,
                expected_previous_token=None)),
        ))
        .set_should_end_session(True)
        .response
    )


class LaunchRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        return play_audio(handler_input, "再生します。")


class ResumeIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.ResumeIntent")(handler_input)

    def handle(self, handler_input):
        # 最初の段階では最初から再生し直す(再生位置の保存は後で追加)
        return play_audio(handler_input, "再開します。")


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


class AudioPlayerEventHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackStarted などのイベントは、何も返さず受け流す"""
    def can_handle(self, handler_input):
        return handler_input.request_envelope.request.object_type.startswith(
            "AudioPlayer.")

    def handle(self, handler_input):
        logger.info("AudioPlayer event: %s",
                    handler_input.request_envelope.request.object_type)
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
sb.add_request_handler(ResumeIntentHandler())
sb.add_request_handler(StopHandler())
sb.add_request_handler(AudioPlayerEventHandler())
sb.add_request_handler(SessionEndedRequestHandler())
sb.add_exception_handler(CatchAllExceptionHandler())

lambda_handler = sb.lambda_handler()
