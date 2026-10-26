package ai.alumbra.app

/**
 * Every spoken or user-visible line in one file. Charles reviews wording
 * here; no UI string lives anywhere else (layouts reference descriptions
 * via strings.xml, which mirrors these).
 */
object Spoken {
    // Escalation line — spoken VERBATIM, single constant, one place.
    const val ESCALATE_LINE =
        "I'm not sure I can help you, hold on while I try to get a hold " +
            "on someone to help us"

    const val RECORDING_START = "Recording. Move your phone slowly across " +
        "what you want to ask about."
    const val PHOTO_TAKEN = "Photo taken. Listening for your question."
    const val CLIP_SENT = "Clip sent, analyzing."
    const val PHOTO_SENT = "Sent, analyzing."
    const val ANSWER_ARRIVED = "Answer arrived."
    const val CANCELLED = "Cancelled."
    const val ERROR_PREFIX = "Sorry, something went wrong. "
    const val NO_ANSWER = "No answer yet. Ask something first."
    const val PERMISSIONS_NEEDED =
        "Alumbra needs camera and microphone access to work."
    const val PERMISSIONS_NEEDED_LAN =
        "Alumbra needs camera, microphone, and nearby-devices access " +
            "to work."
    const val RECOGNIZER_UNAVAILABLE =
        "Voice recognition is not available. Type your question instead."
    const val GETTING_HELP = "Getting help."
    const val DIDNT_CATCH_QUESTION =
        "I didn't catch a question, so I described the scene."

    fun youAsked(question: String): String = "You asked: $question"
    fun iHeard(transcript: String): String = "I heard: $transcript"
    const val WHAT_I_SAW_SHOW = "What I saw. Show details."
    const val WHAT_I_SAW_HIDE = "What I saw. Hide details."
    const val ASK_LIVE_IDLE = "Hold the button to record a clip, or tap " +
        "it for a photo question."

    fun errorDetail(cause: String): String = ERROR_PREFIX + cause
}
