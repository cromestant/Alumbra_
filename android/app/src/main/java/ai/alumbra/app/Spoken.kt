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
    const val VOICE_UNAVAILABLE = "Voice unavailable."
    // Follow-up mode, announced on entry and shown in the banner.
    fun followupIntro(prior: String?): String =
        if (prior.isNullOrBlank()) "Follow-up. Hold and talk to ask."
        else "Follow-up on: $prior. Hold and talk to ask."
    const val FOLLOWUP_LISTENING = "Listening for your follow-up question."
    const val FOLLOWUP_SENT = "Follow-up sent, analyzing."
    const val FOLLOWUP_NONE = "Say or type your follow-up question first."
    const val ASK_EXPIRED =
        "That ask has expired. Please ask again as a new question."
    const val MEDIA_GONE =
        "That ask's media is gone. Please make a fresh ask."
    const val TORCH_ON = "Flashlight on."
    const val TORCH_OFF = "Flashlight off."
    const val CAMERA_UNAVAILABLE = "Camera unavailable."
    const val PHOTO_FAILED = "Photo failed. Try again."
    const val CLIP_FAILED = "Clip failed. Try again."
    const val RECORDING_FAILED = "Recording failed. Try again."
    const val SERVER_UNREACHABLE = "Could not reach the server."
    const val SERVER_ERROR = "The server had a problem."
    // A pipeline stage failed after its retry (HTTP 503) or the model
    // API was unreachable (502): spoken as-is, no "Sorry" wrap.
    const val TRY_AGAIN = "Something went wrong — please try again."
    const val BACK_TO_ASK =
        "New question. Hold the button to record a clip, or tap it " +
            "for a photo question."
    const val BAD_RESPONSE = "Couldn't read the server's answer."

    fun workingSub(clip: Boolean): String =
        (if (clip) "Clip sent. " else "Photo sent. ") +
            "This can take up to a minute — I'll speak as soon as I know."
    const val WHAT_I_SAW_SHOW = "What I saw. Show details."
    const val WHAT_I_SAW_HIDE = "What I saw. Hide details."
    const val ASK_LIVE_IDLE = "Hold the button to record a clip, or tap " +
        "it for a photo question."

    fun errorDetail(cause: String): String = ERROR_PREFIX + cause
}
