package ai.alumbra.app

import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.speech.tts.TextToSpeech
import android.util.Log
import java.io.File
import java.util.Locale

/**
 * The app's one voice channel: an ordered queue of utterances, each a
 * bundled clip (fixed state lines, rendered in the product voice), the
 * answer's server audio, or phone TTS. One plays at a time, in order —
 * a clip never overlaps answer audio or TTS. speak() flushes, then
 * plays; speakQueued() appends.
 *
 * Text is matched against the bundled clip manifest (assets/voice): an
 * exact fixed line plays its clip, anything else is phone TTS. A
 * missing or corrupt clip or answer file falls back to TTS of the same
 * words — degraded voice, never silence. Settings rate applies to both.
 *
 * Init-safe: TTS binds asynchronously; TTS items wait at the head of the
 * queue until it is ready (clips do not wait). If init fails,
 * [onVoiceUnavailable] fires once and TTS items are dropped — clips
 * still play.
 */
class AlumbraTts(context: Context) {
    private var tts: TextToSpeech? = null
    private val appContext = context.applicationContext
    private val main = Handler(Looper.getMainLooper())

    /**
     * Audio discipline: the mic and the app voice never run together —
     * a clip's audio is transcribed evidence, so app voice in a recording
     * poisons the ask itself. While hot, speak requests are dropped
     * (deferring would speak stale lines after the window closes).
     */
    @Volatile
    var micHot: Boolean = false

    /** Fired once, on no particular thread, if the engine fails to init. */
    var onVoiceUnavailable: (() -> Unit)? = null

    /** A clip (asset or file) with the words to speak if it won't play. */
    private sealed interface Item
    private data class Say(val text: String) : Item
    private data class Clip(
        val asset: String?,
        val file: File?,
        val fallback: List<String>,
    ) : Item

    // Main-thread state only.
    private val queue = ArrayDeque<Item>()
    private var current: Item? = null
    private var currentTtsId: String? = null
    private var player: MediaPlayer? = null
    private var last: Item? = null
    private var ready = false
    private var dead = false
    private var shut = false
    private var unavailableFired = false
    private var utteranceSeq = 0

    init {
        tts = TextToSpeech(appContext) { status ->
            onMain {
                if (shut) return@onMain
                if (status == TextToSpeech.SUCCESS) {
                    ready = true
                    applySettings()
                    watchUtterances()
                    Log.i(TTS_TAG, "engine ready")
                } else {
                    dead = true
                    Log.w(TTS_TAG, "engine init FAILED")
                    fireUnavailable()
                }
                pump()
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

    fun speak(text: String) = enqueue(itemFor(text), flush = true)

    /** Queue after the current utterance (escalation line, then answer). */
    fun speakQueued(text: String) = enqueue(itemFor(text), flush = false)

    /**
     * The answer's server audio (the full spoken script). fallback is
     * that same script as text, spoken by TTS if the file won't play.
     */
    fun playFile(file: File, fallback: List<String>) =
        enqueue(Clip(null, file, fallback), flush = true)

    fun replay() {
        last?.let { enqueue(it, flush = true) }
    }

    fun stop() = onMain { flushLocked() }

    fun shutdown() = onMain {
        shut = true
        flushLocked()
        tts?.shutdown()
        tts = null
    }

    private fun itemFor(text: String): Item =
        VoiceClips.assetFor(appContext, text)?.let {
            Clip(it, null, listOf(text))
        } ?: Say(text)

    private fun enqueue(item: Item, flush: Boolean) {
        if (micHot) {
            Log.i(TTS_TAG, "dropped-hot: ${describe(item)}")
            return
        }
        onMain {
            if (shut) return@onMain
            if (flush) {
                flushLocked()
                last = item
            }
            queue.addLast(item)
            Log.i(TTS_TAG, (if (flush) "speak: " else "queued: ") + describe(item))
            pump()
        }
    }

    private fun flushLocked() {
        queue.clear()
        current = null
        currentTtsId = null
        player?.let {
            try {
                it.stop()
            } catch (e: Exception) {
            }
            it.release()
        }
        player = null
        tts?.stop()
    }

    /** Start the head of the queue if nothing is playing. */
    private fun pump() {
        if (current != null || shut) return
        val item = queue.removeFirstOrNull() ?: return
        when (item) {
            is Say -> {
                if (dead) {
                    Log.i(TTS_TAG, "dropped-dead: ${item.text}")
                    pump()
                    return
                }
                if (!ready) {
                    queue.addFirst(item)  // waits for init
                    return
                }
                current = item
                applySettings()
                val id = "alumbra-${++utteranceSeq}"
                currentTtsId = id
                tts?.speak(item.text, TextToSpeech.QUEUE_ADD, null, id)
            }
            is Clip -> {
                current = item
                if (!startClip(item)) fallBack(item)
            }
        }
    }

    private fun startClip(item: Clip): Boolean {
        val mp = MediaPlayer()
        return try {
            mp.setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
            )
            if (item.asset != null) {
                appContext.assets.openFd(item.asset).use {
                    mp.setDataSource(it.fileDescriptor, it.startOffset, it.length)
                }
            } else {
                mp.setDataSource(item.file!!.absolutePath)
            }
            mp.setOnCompletionListener { done(item, mp) }
            mp.setOnErrorListener { _, what, extra ->
                Log.w(TTS_TAG, "clip error $what/$extra: ${describe(item)}")
                if (current === item) {
                    release(mp)
                    fallBack(item)
                }
                true
            }
            mp.prepare()
            val rate = Prefs.speechRate(appContext)
            if (rate != 1.0f) {
                mp.playbackParams = mp.playbackParams.setSpeed(rate)
            }
            mp.start()
            player = mp
            true
        } catch (e: Exception) {
            Log.w(TTS_TAG, "clip failed (${e.javaClass.simpleName}): " +
                describe(item))
            mp.release()
            false
        }
    }

    /** The clip won't play: its words, by TTS, in its place. */
    private fun fallBack(item: Clip) {
        current = null
        Log.i(TTS_TAG, "fallback-tts: ${describe(item)}")
        for (text in item.fallback.asReversed()) queue.addFirst(Say(text))
        pump()
    }

    private fun done(item: Item, mp: MediaPlayer?) {
        if (current !== item) return
        if (mp != null) release(mp)
        current = null
        currentTtsId = null
        pump()
    }

    private fun release(mp: MediaPlayer) {
        if (player === mp) player = null
        mp.release()
    }

    private fun describe(item: Item): String = when (item) {
        is Say -> item.text
        is Clip -> (item.asset ?: item.file?.name ?: "?") +
            " (${item.fallback.joinToString(" ").take(60)})"
    }

    private fun onMain(block: () -> Unit) {
        if (Looper.myLooper() == Looper.getMainLooper()) block()
        else main.post(block)
    }

    private fun fireUnavailable() {
        if (unavailableFired) return
        unavailableFired = true
        try {
            onVoiceUnavailable?.invoke()
        } catch (e: Exception) {
        }
    }

    /** Engine-confirmed speech events advance the queue. */
    private fun watchUtterances() {
        tts?.setOnUtteranceProgressListener(
            object : android.speech.tts.UtteranceProgressListener() {
                override fun onStart(id: String) {
                    Log.i(TTS_TAG, "started $id")
                }

                override fun onDone(id: String) {
                    Log.i(TTS_TAG, "done $id")
                    advance(id)
                }

                @Deprecated("deprecated in API")
                override fun onError(id: String) {
                    Log.w(TTS_TAG, "error $id")
                    advance(id)
                }
            }
        )
    }

    private fun advance(id: String) = onMain {
        val item = current
        if (item is Say && currentTtsId == id) done(item, null)
    }

    companion object {
        const val TTS_TAG = "AlumbraTts"
    }
}

/** Bundled fixed-line clips: assets/voice/manifest.json, text -> id. */
object VoiceClips {
    @Volatile
    private var byText: Map<String, String>? = null

    fun assetFor(context: Context, text: String): String? {
        val map = byText ?: load(context).also { byText = it }
        return map[text]?.let { "voice/$it.mp3" }
    }

    private fun load(context: Context): Map<String, String> = try {
        val lines = org.json.JSONObject(
            context.assets.open("voice/manifest.json").bufferedReader()
                .use { it.readText() }
        ).getJSONObject("lines")
        lines.keys().asSequence().associateBy { lines.getString(it) }
    } catch (e: Exception) {
        Log.w(AlumbraTts.TTS_TAG, "no voice manifest: ${e.message}")
        emptyMap()
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
