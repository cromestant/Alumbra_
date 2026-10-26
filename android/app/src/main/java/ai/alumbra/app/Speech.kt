package ai.alumbra.app

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.speech.tts.TextToSpeech
import java.util.Locale

/** Text-to-speech honoring the Settings rate/language. Remembers the last
 * utterance so Replay works from anywhere. */
class AlumbraTts(context: Context) {
    private var tts: TextToSpeech? = null
    private var lastUtterance: String? = null
    private val appContext = context.applicationContext

    init {
        tts = TextToSpeech(appContext) { status ->
            if (status == TextToSpeech.SUCCESS) {
                applySettings()
            }
        }
    }

    private fun locale(): Locale =
        if (Prefs.language(appContext) == Prefs.LANG_SPANISH) {
            Locale("es", "ES")
        } else {
            Locale.US
        }

    private fun applySettings() {
        val engine = tts ?: return
        val result = engine.setLanguage(locale())
        if (result == TextToSpeech.LANG_MISSING_DATA ||
            result == TextToSpeech.LANG_NOT_SUPPORTED
        ) {
            engine.language = Locale.US
        }
        engine.setSpeechRate(Prefs.speechRate(appContext))
    }

    fun speak(text: String) {
        lastUtterance = text
        applySettings()
        tts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, "alumbra-1")
    }

    /** Queue after the current utterance (escalation line, then answer). */
    fun speakQueued(text: String) {
        applySettings()
        tts?.speak(text, TextToSpeech.QUEUE_ADD, null, "alumbra-${text.hashCode()}")
    }

    fun replay() {
        lastUtterance?.let { speak(it) }
    }

    fun stop() {
        tts?.stop()
    }

    fun shutdown() {
        tts?.stop()
        tts?.shutdown()
        tts = null
    }
}

/**
 * On-device SpeechRecognizer session for one ask window. Calls onResult
 * with the best transcript (possibly empty), exactly once.
 */
class QuestionRecognizer(
    private val context: Context,
    private val onResult: (String) -> Unit,
) {
    private var recognizer: SpeechRecognizer? = null
    private var finished = false

    fun start() {
        if (!SpeechRecognizer.isRecognitionAvailable(context)) {
            finish("")
            return
        }
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(
                RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                RecognizerIntent.LANGUAGE_MODEL_FREE_FORM,
            )
            putExtra(
                RecognizerIntent.EXTRA_LANGUAGE,
                if (Prefs.language(context) == Prefs.LANG_SPANISH) "es-ES"
                else "en-US",
            )
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
        }
        val listener = object : RecognitionListener {
            override fun onResults(results: Bundle) {
                val heard = results.getStringArrayList(
                    SpeechRecognizer.RESULTS_RECOGNITION
                )?.firstOrNull().orEmpty()
                finish(heard)
            }

            override fun onError(error: Int) {
                finish("")
            }

            override fun onReadyForSpeech(params: Bundle) = Unit
            override fun onBeginningOfSpeech() = Unit
            override fun onRmsChanged(rmsdB: Float) = Unit
            override fun onBufferReceived(buffer: ByteArray) = Unit
            override fun onEndOfSpeech() = Unit
            override fun onPartialResults(partialResults: Bundle) = Unit
            override fun onEvent(eventType: Int, params: Bundle) = Unit
        }
        try {
            recognizer = SpeechRecognizer.createSpeechRecognizer(context)
            recognizer?.setRecognitionListener(listener)
            recognizer?.startListening(intent)
        } catch (e: Exception) {
            finish("")
        }
    }

    fun stop() {
        try {
            recognizer?.stopListening()
        } catch (e: Exception) {
            finish("")
        }
    }

    fun destroy() {
        try {
            recognizer?.cancel()
            recognizer?.destroy()
        } catch (e: Exception) {
        }
        recognizer = null
    }

    private fun finish(transcript: String) {
        if (finished) return
        finished = true
        destroy()
        onResult(transcript.trim())
    }
}
