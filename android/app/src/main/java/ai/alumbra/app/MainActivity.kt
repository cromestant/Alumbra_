package ai.alumbra.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.media.MediaMetadataRetriever
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.MotionEvent
import android.view.Surface
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.video.FileOutputOptions
import androidx.camera.video.Quality
import androidx.camera.video.QualitySelector
import androidx.camera.video.Recorder
import androidx.camera.video.Recording
import androidx.camera.video.VideoCapture
import androidx.camera.video.VideoRecordEvent
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import java.io.File
import java.io.FileOutputStream

/**
 * Ask screen (home) + Working state.
 *
 * The giant button keeps its WhatsApp-style interaction: tap = photo ask
 * (still + ~4s live-transcribed audio window), press-and-hold = clip ask
 * (video while held; the server transcribes the clip's own audio). Typed text, when
 * non-empty, wins over the transcript. While the mic is live the button
 * turns red and says so. App voice speaks answers and true state only
 * (sent/analyzing, errors, escalation); screen and button narration is
 * status text in a live region, which TalkBack reads — the app does not
 * narrate its own chrome. The screen is fully operable eyes-closed.
 */
class MainActivity : AppCompatActivity(), SensorEventListener {

    private lateinit var recordButton: Button
    private lateinit var statusText: TextView
    private lateinit var typeField: EditText
    private lateinit var lastAnswerText: TextView
    private lateinit var replayButton: Button
    private lateinit var torchButton: Button
    private lateinit var workingOverlay: LinearLayout
    private lateinit var workingText: TextView
    private lateinit var workingSub: TextView
    private lateinit var pulseView: TextView
    private var pulseAnim: android.animation.ObjectAnimator? = null

    private lateinit var tts: AlumbraTts
    private lateinit var api: AskApi
    private val handler = Handler(Looper.getMainLooper())

    private var imageCapture: ImageCapture? = null
    private var videoCapture: VideoCapture<Recorder>? = null
    private var camera: Camera? = null

    private var clipRecording: Recording? = null
    private var clipFile: File? = null
    private var clipFinalized = false
    private var clipError: String? = null
    private var recognizer: QuestionRecognizer? = null
    private var transcriptArrived = false
    private var sending = false

    private var touchClick = false

    private var sensorManager: SensorManager? = null
    private var torchOn = false
    private var manualTorch = false
    private var lastAnswer: String? = null

    // Follow-up mode: re-ask on the cached ask's media (no camera).
    // Same gesture as the main screen: hold and talk, release to send;
    // a tap (or TalkBack activate) opens a timed listen window instead.
    private var followupAskId: String? = null
    private var followupHolding = false
    // Torch lifecycle: the torch's life is the capture's life.
    private var captureActive = false
    private var lastLux = Float.MAX_VALUE
    // Device-frame gravity, latest and at the shutter: the photo's pitch
    // for metric distance (CaptureGeometry).
    private var lastGravity: FloatArray? = null
    private var shutterGravity: FloatArray? = null

    private val holdRunnable = Runnable { beginClipRecording() }
    private val followupHoldRunnable = Runnable { beginFollowupHold() }

    override fun onCreate(savedInstanceState: Bundle?) {
        Prefs.applyTheme(this)
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        Ui.applyEdgeToEdge(this)

        recordButton = findViewById(R.id.record_button)
        statusText = findViewById(R.id.status_text)
        typeField = findViewById(R.id.type_field)
        lastAnswerText = findViewById(R.id.last_answer_text)
        replayButton = findViewById(R.id.replay_button)
        torchButton = findViewById(R.id.torch_button)
        workingOverlay = findViewById(R.id.working_overlay)
        workingText = findViewById(R.id.working_text)
        workingSub = findViewById(R.id.working_sub)
        pulseView = findViewById(R.id.pulse_view)
        workingOverlay.isClickable = true
        Ui.styleAppName(findViewById(R.id.appname_text))
        styleAskButton()

        tts = AlumbraTts(this)
        // Dead engine: the visible + live-region status text says so
        // (TalkBack speaks it); app voice cannot.
        tts.onVoiceUnavailable = {
            runOnUiThread { statusOnly(Spoken.VOICE_UNAVAILABLE) }
        }
        api = AskApi(Prefs.baseUrl(this))
        sensorManager =
            getSystemService(SENSOR_SERVICE) as SensorManager

        // TalkBack "activate" fires onClick without touch events, so a
        // click listener is the eyes-free path (photo ask). Real taps also
        // raise click via performClick below; touchClick dedupes them.
        recordButton.setOnClickListener {
            if (!touchClick) {
                if (followupAskId != null) doFollowupAsk() else doPhotoAsk()
            }
            touchClick = false
        }
        recordButton.setOnTouchListener { view, event ->
            when (event.action) {
                MotionEvent.ACTION_DOWN -> {
                    if (followupAskId != null) {
                        recordButton.postDelayed(
                            followupHoldRunnable, HOLD_THRESHOLD_MS
                        )
                    } else {
                        ensurePermissions {
                            recordButton.postDelayed(
                                holdRunnable, HOLD_THRESHOLD_MS
                            )
                        }
                    }
                    true
                }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    recordButton.removeCallbacks(holdRunnable)
                    recordButton.removeCallbacks(followupHoldRunnable)
                    if (followupAskId != null) {
                        if (followupHolding) endFollowupHold()
                        else doFollowupAsk()
                    } else if (clipRecording != null) {
                        finishClipAsk()
                    } else {
                        doPhotoAsk()
                    }
                    touchClick = true
                    view.performClick()
                    true
                }
                else -> false
            }
        }

        findViewById<Button>(R.id.cancel_button).setOnClickListener {
            api.cancel()
            endCapture()
            hideWorking()
            statusOnly(Spoken.CANCELLED)
            Haptics.pulse(this)
        }
        replayButton.setOnClickListener {
            val answer = lastAnswer
            if (answer != null) {
                Haptics.pulse(this)
                tts.speak(answer)
            } else {
                statusOnly(Spoken.NO_ANSWER)
            }
        }
        torchButton.setOnClickListener {
            manualTorch = true
            setTorch(!torchOn)
            Haptics.pulse(this)
            statusOnly(if (torchOn) Spoken.TORCH_ON else Spoken.TORCH_OFF)
        }
        findViewById<Button>(R.id.history_button).setOnClickListener {
            startActivity(Intent(this, HistoryActivity::class.java))
        }
        findViewById<Button>(R.id.settings_button).setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }

        findViewById<Button>(R.id.followup_exit_button).setOnClickListener {
            Haptics.pulse(this)
            goToFreshAsk()
        }

        followupAskId = intent.getStringExtra(EXTRA_FOLLOWUP_ASK_ID)
        if (followupAskId != null) {
            // No camera in follow-up mode: the cached ask's media is used.
            val prior = intent.getStringExtra(EXTRA_FOLLOWUP_QUESTION)
            styleFollowupButton()
            findViewById<LinearLayout>(R.id.followup_panel).visibility =
                LinearLayout.VISIBLE
            findViewById<TextView>(R.id.followup_question_text).text =
                prior?.takeUnless { it.isBlank() } ?: "your last answer"
            recordButton.contentDescription =
                getString(R.string.followup_button_description)
            statusOnly(Spoken.followupIntro(prior))
        } else {
            if (savedInstanceState == null &&
                intent.getBooleanExtra(EXTRA_NEW_QUESTION, false)
            ) {
                statusOnly(Spoken.BACK_TO_ASK)
            }
            ensurePermissions {
                bindCamera()
                runProbeOnce()
            }
        }
    }

    /** Arriving back from an Answer ("New question" / Back). */
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        // A new question starts empty: a typed question left from the
        // last ask would otherwise be resent silently.
        typeField.setText("")
        if (followupAskId == null) statusOnly(Spoken.BACK_TO_ASK)
    }

    override fun onResume() {
        super.onResume()
        api = AskApi(Prefs.baseUrl(this))
        sensorManager?.getDefaultSensor(Sensor.TYPE_LIGHT)?.let {
            sensorManager?.registerListener(
                this, it, SensorManager.SENSOR_DELAY_NORMAL
            )
        }
        (sensorManager?.getDefaultSensor(Sensor.TYPE_GRAVITY)
            ?: sensorManager?.getDefaultSensor(Sensor.TYPE_ACCELEROMETER))
            ?.let {
                sensorManager?.registerListener(
                    this, it, SensorManager.SENSOR_DELAY_UI
                )
            }
        refreshLastAnswer()
    }

    override fun onPause() {
        super.onPause()
        endCapture()
        sensorManager?.unregisterListener(this)
    }

    override fun onDestroy() {
        recordButton.removeCallbacks(holdRunnable)
        recordButton.removeCallbacks(followupHoldRunnable)
        handler.removeCallbacksAndMessages(null)
        recognizer?.destroy()
        api.cancel()
        endCapture()
        tts.shutdown()
        Earcons.release()
        super.onDestroy()
    }

    // -- permissions ----------------------------------------------------

    /**
     * Camera + mic, plus ACCESS_LOCAL_NETWORK on API 37+ — the local
     * network gate is enforced silently there, so without the runtime
     * grant (shown as "Nearby devices") LAN requests look like a dead
     * network.
     *
     * Retry-on-grant is deliberately partial: camera bind + probe re-run
     * automatically, and the grant handler announces readiness — but a
     * touch-initiated capture is never auto-fired, since the user's
     * finger is gone by grant time. The user re-taps to ask.
     *
     * The LAN permission is only requested when the configured backend
     * is a local address. A cloud host (public DNS, the prod shape)
     * never triggers the "Nearby devices" prompt; once prod is
     * cloud-only the manifest line can go too.
     */
    private fun needsLanPermission(): Boolean {
        if (Build.VERSION.SDK_INT < 37) return false
        val host = try {
            android.net.Uri.parse(Prefs.baseUrl(this)).host ?: return true
        } catch (e: Exception) {
            return true
        }
        val h = host.lowercase()
        if (h == "localhost" || h.endsWith(".localhost") ||
            h.endsWith(".local") || h == "::1"
        ) {
            return true
        }
        if (h.length >= 2 && h[0] == 'f' && (h[1] == 'c' || h[1] == 'd') &&
            h.contains(":")
        ) {
            return true // IPv6 unique-local (fc00::/7)
        }
        if (h.startsWith("fe80:")) return true // IPv6 link-local
        val v4 = h.split(".")
        if (v4.size == 4) {
            val octets = v4.map { it.toIntOrNull() ?: return true }
            if (octets[0] == 10) return true
            if (octets[0] == 172 && octets[1] in 16..31) return true
            if (octets[0] == 192 && octets[1] == 168) return true
            if (octets[0] == 169 && octets[1] == 254) return true
            if (octets[0] == 100 && octets[1] in 64..127) return true
        }
        return false
    }

    private fun requiredPermissions(): Array<String> {
        val perms = mutableListOf(
            Manifest.permission.CAMERA, Manifest.permission.RECORD_AUDIO
        )
        if (needsLanPermission()) {
            perms.add("android.permission.ACCESS_LOCAL_NETWORK")
        }
        return perms.toTypedArray()
    }

    private fun ensurePermissions(onGranted: () -> Unit) {
        val needed = requiredPermissions().filter {
            ContextCompat.checkSelfPermission(this, it) !=
                PackageManager.PERMISSION_GRANTED
        }.toTypedArray()
        if (needed.isEmpty()) {
            onGranted()
        } else {
            statusOnly(
                if (needsLanPermission()) {
                    Spoken.PERMISSIONS_NEEDED_LAN
                } else {
                    Spoken.PERMISSIONS_NEEDED
                }
            )
            ActivityCompat.requestPermissions(this, needed, REQUEST_CODE)
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(
            requestCode, permissions, grantResults
        )
        if (requestCode != REQUEST_CODE) return
        if (grantResults.isNotEmpty() && grantResults.all {
                it == PackageManager.PERMISSION_GRANTED
            }
        ) {
            statusOnly(Spoken.ASK_LIVE_IDLE)
            bindCamera()
            runProbeOnce()
        } else {
            announce(
                if (needsLanPermission()) {
                    Spoken.PERMISSIONS_NEEDED_LAN
                } else {
                    Spoken.PERMISSIONS_NEEDED
                }
            )
        }
    }

    // -- camera ----------------------------------------------------------

    private fun bindCamera() {
        // No preview surface (mock look): capture-only binding.
        val providerFuture = ProcessCameraProvider.getInstance(this)
        providerFuture.addListener({
            try {
                val provider = providerFuture.get()
                imageCapture = ImageCapture.Builder().build()
                val recorder = Recorder.Builder()
                    .setQualitySelector(
                        QualitySelector.fromOrderedList(
                            listOf(Quality.HD, Quality.SD)
                        )
                    )
                    .build()
                videoCapture = VideoCapture.withOutput(recorder)
                provider.unbindAll()
                camera = provider.bindToLifecycle(
                    this,
                    CameraSelector.DEFAULT_BACK_CAMERA,
                    imageCapture,
                    videoCapture,
                )
            } catch (e: Exception) {
                Log.e("Alumbra", "bindCamera failed", e)
                announce(Spoken.CAMERA_UNAVAILABLE)
            }
        }, ContextCompat.getMainExecutor(this))
    }

    private fun displayRotation(): Int {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            display?.rotation ?: Surface.ROTATION_0
        } else {
            @Suppress("DEPRECATION")
            windowManager.defaultDisplay.rotation
        }
    }

    private fun runProbeOnce() {
        if (Prefs.probed(this)) return
        CapabilityProbe.probe(this) { /* persisted + logged; see Settings */ }
    }

    // -- torch ------------------------------------------------------------
    // The torch's life is the capture's life: auto-torch may turn on
    // when capture starts (in the dark) and goes out the moment capture
    // ends — sent, cancelled, failed, or left. Nothing burns into
    // Working/Answer. The manual toggle is the user's own outside asks.

    private fun setTorch(on: Boolean) {
        torchOn = on
        Log.i("Alumbra", "torch ${if (on) "on" else "off"}")
        try {
            camera?.cameraControl?.enableTorch(on)
        } catch (e: Exception) {
        }
        torchButton.contentDescription = getString(
            if (on) R.string.torch_description_on
            else R.string.torch_description_off
        )
    }

    private fun beginCapture() {
        captureActive = true
        if (!manualTorch && lastLux < DARK_LUX) {
            setTorch(true)
        }
    }

    private fun endCapture() {
        captureActive = false
        manualTorch = false
        if (torchOn) {
            setTorch(false)
        }
    }

    override fun onSensorChanged(event: SensorEvent) {
        if (event.sensor.type == Sensor.TYPE_GRAVITY ||
            event.sensor.type == Sensor.TYPE_ACCELEROMETER
        ) {
            lastGravity = event.values.copyOf(3)
            return
        }
        if (event.sensor.type != Sensor.TYPE_LIGHT) return
        lastLux = event.values[0]
        if (manualTorch || !captureActive) return
        setTorch(lastLux < DARK_LUX)
    }

    override fun onAccuracyChanged(sensor: Sensor, accuracy: Int) = Unit

    // -- photo ask (tap) ---------------------------------------------------

    private fun doPhotoAsk() {
        if (sending || workingOverlay.visibility == LinearLayout.VISIBLE) {
            return
        }
        val capture = imageCapture ?: run {
            announce("Camera is not ready yet.")
            return
        }
        ensurePermissions {
            beginCapture()
            capture.targetRotation = displayRotation()
            val file = File(
                cacheDir, "ask_${System.currentTimeMillis()}.jpg"
            )
            shutterGravity = lastGravity?.copyOf()
            capture.takePicture(
                ImageCapture.OutputFileOptions.Builder(file).build(),
                ContextCompat.getMainExecutor(this),
                object : ImageCapture.OnImageSavedCallback {
                    override fun onCaptureStarted() {
                        // The shutter: the pitch the photo was taken at.
                        shutterGravity = lastGravity?.copyOf()
                    }

                    override fun onImageSaved(
                        output: ImageCapture.OutputFileResults
                    ) {
                        // Shot taken: the torch's job is done even
                        // though the listen window still runs.
                        endCapture()
                        // Mic goes hot for the listen window: cut any
                        // in-flight utterance, confirm with haptic + cue.
                        tts.stop()
                        tts.micHot = true
                        Haptics.pulse(this@MainActivity)
                        Earcons.shutter()
                        statusOnly(Spoken.PHOTO_TAKEN)
                        showMicLive(hold = false)
                        listenThenSend(photoFile = file, meta = captureMeta(file))
                    }

                    override fun onError(exc: ImageCaptureException) {
                        tts.micHot = false
                        endCapture()
                        Log.e("Alumbra", "photo capture failed", exc)
                        announce(Spoken.PHOTO_FAILED)
                    }
                },
            )
        }
    }

    /** ~4s live-transcribed window, then send the photo ask. */
    /** Pitch at the shutter + focal length in upload px, or null. */
    private fun captureMeta(file: File): CaptureMeta? {
        val pitch = CaptureGeometry.pitchDownDeg(shutterGravity)
        val cam = camera
        val capture = imageCapture
        val focal = if (cam != null && capture != null) {
            CaptureGeometry.focalPx(cam, capture, file)
        } else null
        Log.i("Alumbra", "capture meta pitch_deg=$pitch focal_px=$focal")
        return if (pitch != null && focal != null) {
            CaptureMeta(pitch, focal)
        } else null
    }

    private fun listenThenSend(photoFile: File, meta: CaptureMeta?) {
        sending = true
        transcriptArrived = false
        recognizer = QuestionRecognizer(this) { transcript ->
            if (transcriptArrived) return@QuestionRecognizer
            transcriptArrived = true
            tts.micHot = false
            showMicIdle()
            sendPhotoAsk(photoFile, transcript, meta)
        }
        recognizer?.start()
        handler.postDelayed({
            if (!transcriptArrived) {
                recognizer?.stop()
                // onError("") arrives and completes the send.
            }
        }, LISTEN_WINDOW_MS)
    }

    private fun sendPhotoAsk(
        photoFile: File,
        transcript: String,
        meta: CaptureMeta?,
    ) {
        val typed = typeField.text.toString().trim()
        val question = typed.ifEmpty { transcript }.ifEmpty { null }
        // No "I heard" echo: the question is shown on the Answer screen
        // and in History, never read back.
        showWorking(Spoken.PHOTO_SENT, Spoken.workingSub(false))
        // No raw-audio file for photo asks: SpeechRecognizer owns the mic
        // during the window, and concurrent capture is unreliable.
        api.ask(photoFile, question, audio = null, media = null, meta = meta) {
            onAskOutcome(question, it)
        }
    }

    // -- clip ask (hold) -----------------------------------------------------

    private fun beginClipRecording() {
        if (sending || workingOverlay.visibility == LinearLayout.VISIBLE) {
            clipRecording = null
            return
        }
        val video = videoCapture ?: return
        ensurePermissions {
            // Cut any in-flight utterance before the mic opens.
            tts.stop()
            tts.micHot = true
            beginCapture()
            clipFile = File(cacheDir, "ask_${System.currentTimeMillis()}.mp4")
            clipFinalized = false
            clipError = null
            if (ContextCompat.checkSelfPermission(
                    this, Manifest.permission.RECORD_AUDIO
                ) != PackageManager.PERMISSION_GRANTED
            ) {
                announce(Spoken.PERMISSIONS_NEEDED)
                return@ensurePermissions
            }
            try {
                clipRecording = video.output
                    .prepareRecording(
                        this,
                        FileOutputOptions.Builder(clipFile!!).build(),
                    )
                    .withAudioEnabled()
                    .start(ContextCompat.getMainExecutor(this)) { event ->
                        if (event is VideoRecordEvent.Finalize) {
                            if (event.hasError()) {
                                // Technical text: History detail + log only.
                                // The status line gets plain words below.
                                Log.w(
                                    "Alumbra",
                                    "clip finalize error=${event.error}",
                                )
                                clipError = "Clip failed " +
                                    "(${event.error})."
                            }
                            clipFinalized = true
                            maybeSendClipAsk()
                        }
                    }
                // Start cue (and the stop cue on release) are ours. No
                // live recognizer on clips: it shared the mic with the
                // recorder, mostly heard nothing, ended on the first pause
                // with the speech engine's own beep, and a fragment it did
                // catch would have replaced the full question. The clip's
                // audio holds everything said while held; the server
                // transcribes it.
                Haptics.pulse(this)
                Earcons.start()
                showMicLive(hold = true)
                statusOnly(Spoken.RECORDING_START)
            } catch (e: Exception) {
                clipRecording = null
                tts.micHot = false
                showMicIdle()
                endCapture()
                Log.e("Alumbra", "clip recording failed", e)
                announce(Spoken.RECORDING_FAILED)
            }
        }
    }

    private fun finishClipAsk() {
        try {
            clipRecording?.stop()
        } catch (e: Exception) {
        }
        clipRecording = null
        // Mic closed: stop cue now, speech safe from here on.
        tts.micHot = false
        showMicIdle()
        // Clip in hand: the torch goes out before Working settles.
        endCapture()
        Haptics.pulse(this)
        Earcons.stop()
        showWorking(Spoken.CLIP_SENT, Spoken.workingSub(true))
        // maybeSendClipAsk fires when the recorder finalizes the file.
        handler.postDelayed({
            if (sending) return@postDelayed
            if (!clipFinalized) {
                clipFinalized = true
                maybeSendClipAsk()
            }
        }, CLIP_SETTLE_MS)
    }

    private fun maybeSendClipAsk() {
        if (sending || !clipFinalized) return
        sending = true
        val error = clipError
        val file = clipFile
        if (error != null || file == null || !file.exists()) {
            hideWorking()
            sending = false
            // History keeps the technical detail; the status line
            // and voice get plain words, never the error code.
            val plain = if (error != null) Spoken.CLIP_FAILED else "Empty clip."
            HistoryStore.add(
                this, null, null, plain, errorDetail = error
            )
            announce(plain)
            return
        }
        val typed = typeField.text.toString().trim()
        // Typed text wins; otherwise the server transcribes the clip.
        val question = typed.ifEmpty { null }
        Thread {
            val frame = extractMiddleFrame(file)
            runOnUiThread {
                if (frame == null) {
                    hideWorking()
                    sending = false
                    val msg = "Could not read the clip."
                    HistoryStore.add(this, question, null, msg)
                    announce(msg)
                    return@runOnUiThread
                }
                // The clip carries its own audio track; attached as media.
                api.ask(frame, question, audio = null, media = file) {
                    onAskOutcome(question, it)
                }
            }
        }.start()
    }

    private fun extractMiddleFrame(clip: File): File? {
        val retriever = MediaMetadataRetriever()
        return try {
            retriever.setDataSource(clip.absolutePath)
            val durationMs = retriever.extractMetadata(
                MediaMetadataRetriever.METADATA_KEY_DURATION
            )?.toLongOrNull() ?: 0L
            val frame: Bitmap = retriever.getFrameAtTime(
                (durationMs / 2) * 1000,
                MediaMetadataRetriever.OPTION_CLOSEST_SYNC,
            ) ?: return null
            val out = File(cacheDir, "frame_${System.currentTimeMillis()}.jpg")
            FileOutputStream(out).use {
                frame.compress(Bitmap.CompressFormat.JPEG, 90, it)
            }
            out
        } catch (e: Exception) {
            null
        } finally {
            try {
                retriever.release()
            } catch (e: Exception) {
            }
        }
    }

    // -- working + outcome ----------------------------------------------------

    private fun showWorking(line: String, sub: String) {
        workingText.text = getString(R.string.working_label)
        workingSub.text = sub
        workingOverlay.visibility = LinearLayout.VISIBLE
        pulseAnim?.cancel()
        pulseAnim = Ui.pulse(pulseView)
        announce(line)
    }

    private fun hideWorking() {
        pulseAnim?.cancel()
        pulseAnim = null
        pulseView.alpha = 1f
        workingOverlay.visibility = LinearLayout.GONE
    }

    private fun styleAskButton() {
        styleRecordButton(
            R.string.record_button_big, R.string.record_button_small
        )
    }

    private fun styleFollowupButton() {
        styleRecordButton(
            R.string.followup_button_big, R.string.followup_button_small
        )
    }

    /** Mic live: red button, its label says so for the whole capture. */
    private fun showMicLive(hold: Boolean) {
        recordButton.setBackgroundResource(R.drawable.ask_button_recording)
        recordButton.setTextColor(
            ContextCompat.getColor(this, R.color.alumbra_rec_ink)
        )
        if (hold) {
            styleRecordButton(
                R.string.recording_button_big,
                R.string.recording_button_small, REC_SMALL_INK,
            )
        } else {
            styleRecordButton(
                R.string.listening_button_big,
                R.string.listening_button_small, REC_SMALL_INK,
            )
        }
        recordButton.contentDescription = getString(
            if (hold) R.string.recording_button_description
            else R.string.listening_button_description
        )
    }

    /** Mic closed: back to the Ask (or follow-up) button. */
    private fun showMicIdle() {
        recordButton.setBackgroundResource(R.drawable.ask_button)
        recordButton.setTextColor(
            ContextCompat.getColor(this, R.color.alumbra_amber_ink)
        )
        if (followupAskId != null) {
            styleFollowupButton()
            recordButton.contentDescription =
                getString(R.string.followup_button_description)
        } else {
            styleAskButton()
            recordButton.contentDescription =
                getString(R.string.record_button_description)
        }
    }

    private fun styleRecordButton(
        bigRes: Int,
        smallRes: Int,
        smallInk: Int = IDLE_SMALL_INK,
    ) {
        val big = getString(bigRes)
        val small = getString(smallRes)
        val text = "$big\n$small"
        val out = android.text.SpannableString(text)
        out.setSpan(
            android.text.style.AbsoluteSizeSpan(30, true), 0, big.length,
            android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE,
        )
        out.setSpan(
            android.text.style.StyleSpan(android.graphics.Typeface.BOLD),
            0, big.length,
            android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE,
        )
        val smallStart = big.length + 1
        out.setSpan(
            android.text.style.AbsoluteSizeSpan(15, true), smallStart,
            text.length, android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE,
        )
        out.setSpan(
            android.text.style.ForegroundColorSpan(smallInk),
            smallStart, text.length,
            android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE,
        )
        recordButton.text = out
    }

    // -- follow-up (same media, new question, no camera) ---------------------

    private fun doFollowupAsk() {
        val askId = followupAskId ?: return
        if (sending || workingOverlay.visibility == LinearLayout.VISIBLE) {
            return
        }
        val typed = typeField.text.toString().trim()
        if (typed.isNotEmpty()) {
            sendFollowup(askId, typed)
            return
        }
        // Tap: a timed listen window (the eyes-free TalkBack path).
        startFollowupListening()
        handler.postDelayed({
            if (!transcriptArrived) {
                recognizer?.stop()
                // onError("") arrives and completes the path.
            }
        }, FOLLOWUP_LISTEN_MS)
    }

    /** Hold: listen while the button is held; release sends. */
    private fun beginFollowupHold() {
        if (followupAskId == null || sending ||
            workingOverlay.visibility == LinearLayout.VISIBLE
        ) {
            return
        }
        if (typeField.text.toString().isNotBlank()) return // tap sends it
        followupHolding = true
        startFollowupListening()
    }

    private fun endFollowupHold() {
        followupHolding = false
        showMicIdle()
        recognizer?.stop()
        // onResults/onError arrives and completes the path.
    }

    private fun startFollowupListening() {
        val askId = followupAskId ?: return
        // Speak the question: recognizer owns the mic, so it runs hot
        // (no app voice into the window) with haptic + cue only.
        tts.stop()
        tts.micHot = true
        Haptics.pulse(this)
        Earcons.start()
        showMicLive(hold = followupHolding)
        statusOnly(Spoken.FOLLOWUP_LISTENING)
        sending = true
        transcriptArrived = false
        recognizer = QuestionRecognizer(this) { transcript ->
            if (transcriptArrived) return@QuestionRecognizer
            transcriptArrived = true
            tts.micHot = false
            showMicIdle()
            Haptics.pulse(this)
            Earcons.stop()
            if (transcript.isBlank()) {
                sending = false
                announce(Spoken.FOLLOWUP_NONE)
            } else {
                sendFollowup(askId, transcript)
            }
        }
        recognizer?.start()
    }

    /**
     * Leave follow-up mode for a fresh ask. CLEAR_TOP cannot be used
     * here: this screen is itself the topmost MainActivity, so the
     * intent would come back to it. A fresh task gives a plain Ask
     * screen with the camera bound; History is persisted, nothing lost.
     */
    private fun goToFreshAsk() {
        tts.stop()
        recognizer?.destroy()
        startActivity(
            Intent(this, MainActivity::class.java)
                .putExtra(EXTRA_NEW_QUESTION, true)
                .addFlags(
                    Intent.FLAG_ACTIVITY_NEW_TASK or
                        Intent.FLAG_ACTIVITY_CLEAR_TASK
                )
        )
    }

    private fun sendFollowup(askId: String, question: String) {
        sending = true
        typeField.setText("")
        showWorking(Spoken.FOLLOWUP_SENT, Spoken.workingSub(false))
        api.followup(askId, question) {
            onAskOutcome(question, it)
        }
    }

    /**
     * Expired follow-up target: become a plain Ask screen in place (so
     * the expiry message is not cut off by navigation). Follow-up mode
     * never bound the camera, so bind it now or the next tap would say
     * "Camera is not ready yet."
     */
    private fun exitFollowupMode() {
        followupAskId = null
        followupHolding = false
        typeField.setText("")
        showMicIdle()
        statusText.text = ""
        findViewById<LinearLayout>(R.id.followup_panel).visibility =
            LinearLayout.GONE
        ensurePermissions { bindCamera() }
    }

    private fun onAskOutcome(question: String?, outcome: AskOutcome) {
        runOnUiThread {
            hideWorking()
            sending = false
            Haptics.pulse(this)
            when (outcome) {
                is AskOutcome.Ok -> {
                    // Display wins: the server's question_used is what the
                    // answer actually addresses (transcripts can differ).
                    val shown = outcome.result.questionUsed ?: question
                    val record = HistoryStore.add(
                        this, shown, outcome.result, null,
                        outcome.result.questionSource,
                    )
                    refreshLastAnswer()
                    // The Answer screen speaks the answer itself.
                    statusOnly(Spoken.ANSWER_ARRIVED)
                    startActivity(
                        Intent(this, AnswerActivity::class.java).putExtra(
                            AnswerActivity.EXTRA_RECORD_ID, record.id
                        )
                    )
                    // A follow-up screen is spent once answered: Back
                    // from the new answer must not land on it.
                    if (followupAskId != null) finish()
                }
                is AskOutcome.Err -> {
                    // Expired follow-up target: back to a fresh ask —
                    // the message already says so plainly, no Sorry-wrap.
                    if (outcome.expired) exitFollowupMode()
                    // History keeps the technical detail; the status
                    // line and voice get plain words only.
                    HistoryStore.add(
                        this, question, null, outcome.message,
                        errorDetail = outcome.detail,
                    )
                    announce(
                        if (outcome.expired || outcome.plain) outcome.message
                        else Spoken.errorDetail(outcome.message)
                    )
                }
            }
        }
    }

    private fun refreshLastAnswer() {
        val record = HistoryStore.list(this).firstOrNull {
            it.answer != null
        }
        val full = record?.answer
        lastAnswer = full
        lastAnswerText.text = when {
            full == null -> getString(R.string.no_answer_yet)
            full.length > LAST_ANSWER_CHARS ->
                full.take(LAST_ANSWER_CHARS) + "…"
            else -> full
        }
    }

    private fun announce(line: String) {
        statusText.text = line
        tts.speak(line)
    }

    /** Live-region status without app voice (used while the mic is hot). */
    private fun statusOnly(line: String) {
        statusText.text = line
    }

    companion object {
        private const val REQUEST_CODE = 42
        private const val HOLD_THRESHOLD_MS = 400L
        private const val LISTEN_WINDOW_MS = 4000L
        private const val FOLLOWUP_LISTEN_MS = 6000L
        private const val CLIP_SETTLE_MS = 10000L
        private const val LAST_ANSWER_CHARS = 120
        private const val DARK_LUX = 10.0f
        private const val IDLE_SMALL_INK = 0xBF161200.toInt()
        private const val REC_SMALL_INK = 0xE6FFFFFF.toInt()
        const val EXTRA_FOLLOWUP_ASK_ID = "followup_ask_id"
        const val EXTRA_FOLLOWUP_QUESTION = "followup_question"
        const val EXTRA_NEW_QUESTION = "new_question"
    }
}
