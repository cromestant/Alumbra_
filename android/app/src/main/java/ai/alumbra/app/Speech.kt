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
 * utterance so Replay works from anywhere.
 *
 * Init-safe: TextToSpeech binds asynchronously, and an utterance issued
 * before onInit completes is silently discarded by the platform. Requests
 * made while the engine is not ready are buffered — preserving order and
 * the flush/queue semantics of speak/speakQueued — and delivered when
 * init succeeds. If init fails, [onVoiceUnavailable] fires once so the
 * activity can say so through the state channel instead of going silent.
 */
class AlumbraTts(context: Context) {
    private var tts: TextToSpeech? = null
    private var lastUtterance: String? = null
    private val appContext = context.applicationContext

    /**
     * Audio discipline: the mic and the app voice never run together —
     * a clip's audio is transcribed evidence, so app TTS in a recording
     * poisons the ask itself. While hot, speak requests are dropped
     * (deferring would speak stale lines after the window closes).
     * The drop happens before buffering: a hot mic never queues.
     */
    @Volatile
    var micHot: Boolean = false

    /** Fired once, on no particular thread, if the engine fails to init. */
    var onVoiceUnavailable: (() -> Unit)? = null

    private val lock = Any()
    private var ready = false
    private var shut = false
    private var unavailableFired = false
    private var pendingFlush: String? = null
    private val pendingQueue = mutableListOf<String>()
    private var utteranceSeq = 0

    init {
        tts = TextToSpeech(appContext) { status ->
            var failed = false
            synchronized(lock) {
                if (shut) return@TextToSpeech
                if (status == TextToSpeech.SUCCESS) {
                    ready = true
                    applySettings()
                    watchUtterances()
                    deliverPendingLocked()
                } else {
                    failed = true
                }
            }
            if (failed) {
                android.util.Log.w(TTS_TAG, "engine init FAILED")
                fireUnavailable()
            } else {
                android.util.Log.i(TTS_TAG, "engine ready")
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
        if (micHot) {
            android.util.Log.i(TTS_TAG, "dropped-hot: $text")
            return
        }
        lastUtterance = text
        synchronized(lock) {
            if (shut) return
            if (!ready) {
                // Flush semantics pre-ready: replace the whole buffer.
                pendingFlush = text
                pendingQueue.clear()
                android.util.Log.i(TTS_TAG, "buffered-flush: $text")
                return
            }
            applySettings()
            tts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, nextIdLocked())
            android.util.Log.i(TTS_TAG, "speak: $text")
        }
    }

    /** Queue after the current utterance (escalation line, then answer). */
    fun speakQueued(text: String) {
        if (micHot) {
            android.util.Log.i(TTS_TAG, "dropped-hot: $text")
            return
        }
        synchronized(lock) {
            if (shut) return
            if (!ready) {
                pendingQueue.add(text)
                android.util.Log.i(TTS_TAG, "buffered-queue: $text")
                return
            }
            applySettings()
            tts?.speak(text, TextToSpeech.QUEUE_ADD, null, nextIdLocked())
            android.util.Log.i(TTS_TAG, "queued: $text")
        }
    }

    fun replay() {
        lastUtterance?.let { speak(it) }
    }

    fun stop() {
        synchronized(lock) {
            pendingFlush = null
            pendingQueue.clear()
        }
        tts?.stop()
    }

    fun shutdown() {
        synchronized(lock) {
            shut = true
            ready = false
            pendingFlush = null
            pendingQueue.clear()
        }
        tts?.stop()
        tts?.shutdown()
        tts = null
    }

    /** Caller must hold [lock]; engine known ready. */
    private fun deliverPendingLocked() {
        if (micHot) {
            // Init finished mid-capture: evidence rule wins, drop all.
            android.util.Log.i(TTS_TAG, "dropped-hot ${pendingCountLocked()} buffered")
            pendingFlush = null
            pendingQueue.clear()
            return
        }
        pendingFlush?.let {
            tts?.speak(it, TextToSpeech.QUEUE_FLUSH, null, nextIdLocked())
            android.util.Log.i(TTS_TAG, "delivered-flush: $it")
        }
        for (queued in pendingQueue) {
            tts?.speak(queued, TextToSpeech.QUEUE_ADD, null, nextIdLocked())
            android.util.Log.i(TTS_TAG, "delivered-queue: $queued")
        }
        pendingFlush = null
        pendingQueue.clear()
    }

    private fun pendingCountLocked(): Int =
        pendingQueue.size + if (pendingFlush == null) 0 else 1

    private fun nextIdLocked(): String {
        utteranceSeq += 1
        return "alumbra-$utteranceSeq"
    }

    private fun fireUnavailable() {
        val fire = synchronized(lock) {
            if (unavailableFired) false else {
                unavailableFired = true
                true
            }
        }
        if (fire) {
            try {
                onVoiceUnavailable?.invoke()
            } catch (e: Exception) {
            }
        }
    }

    /** Engine-confirmed speech events; also the no-silent-drop evidence. */
    private fun watchUtterances() {
        tts?.setOnUtteranceProgressListener(
            object : android.speech.tts.UtteranceProgressListener() {
                override fun onStart(id: String) {
                    android.util.Log.i(TTS_TAG, "started $id")
                }

                override fun onDone(id: String) {
                    android.util.Log.i(TTS_TAG, "done $id")
                }

                @Deprecated("deprecated in API")
                override fun onError(id: String) {
                    android.util.Log.w(TTS_TAG, "error $id")
                }
            }
        )
    }

    companion object {
        const val TTS_TAG = "AlumbraTts"
    }
}

/**
 * Record cues, not voice: start/stop/shutter confirmations are short
 * tones, safe to fire while the mic is hot.
 */
object Earcons {
    private var tones: android.media.ToneGenerator? = null

    private fun generator(): android.media.ToneGenerator? {
        if (tones == null) {
            tones = try {
                android.media.ToneGenerator(
                    android.media.AudioManager.STREAM_MUSIC, 80
                )
            } catch (e: Exception) {
                null
            }
        }
        return tones
    }

    /** Clip recording started. */
    fun start() {
        try {
            generator()?.startTone(android.media.ToneGenerator.TONE_PROP_BEEP)
        } catch (e: Exception) {
        }
    }

    /** Clip recording stopped. */
    fun stop() {
        try {
            generator()?.startTone(android.media.ToneGenerator.TONE_PROP_BEEP2)
        } catch (e: Exception) {
        }
    }

    /** Photo shutter. */
    fun shutter() {
        try {
            generator()?.startTone(android.media.ToneGenerator.TONE_PROP_ACK)
        } catch (e: Exception) {
        }
    }

    fun release() {
        try {
            tones?.release()
        } catch (e: Exception) {
        }
        tones = null
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
