package ai.alumbra.app

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.VibrationEffect
import android.os.Vibrator
import android.view.MotionEvent
import android.widget.Button
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

/**
 * Alumbra — single-screen capture UI.
 *
 * Accessibility is load-bearing here, not polish: the record button has a
 * contentDescription, status changes are announced through a live-region
 * TextView (plus TextToSpeech once wired), and recording start/stop pulses
 * haptically so there is never a silent dead moment.
 *
 * TODO (hackathon): bind CameraX VideoCapture in startRecording(), POST the
 * finished clip to POST /v1/clips in stopRecordingAndSend(), and speak the
 * backend's answer with TextToSpeech.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var recordButton: Button
    private lateinit var statusText: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        recordButton = findViewById(R.id.record_button)
        statusText = findViewById(R.id.status_text)

        // WhatsApp-voice-note style: press and hold to record, release to send.
        recordButton.setOnTouchListener { _, event ->
            when (event.action) {
                MotionEvent.ACTION_DOWN -> {
                    ensurePermissions { startRecording() }
                    true
                }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    stopRecordingAndSend()
                    true
                }
                else -> false
            }
        }
    }

    private fun ensurePermissions(onGranted: () -> Unit) {
        val needed = arrayOf(Manifest.permission.CAMERA, Manifest.permission.RECORD_AUDIO)
            .filter {
                ContextCompat.checkSelfPermission(this, it) !=
                    PackageManager.PERMISSION_GRANTED
            }
            .toTypedArray()
        if (needed.isEmpty()) {
            onGranted()
        } else {
            ActivityCompat.requestPermissions(this, needed, REQUEST_CODE)
        }
    }

    private fun pulse() {
        val vibrator = getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            vibrator.vibrate(
                VibrationEffect.createOneShot(80, VibrationEffect.DEFAULT_AMPLITUDE)
            )
        } else {
            @Suppress("DEPRECATION")
            vibrator.vibrate(80)
        }
    }

    private fun startRecording() {
        // TODO: bind CameraX VideoCapture and start writing to a temp file.
        pulse()
        statusText.text = getString(R.string.status_recording)
        // TODO: TextToSpeech — "Recording. Move your phone slowly across
        // what you want to ask about."
    }

    private fun stopRecordingAndSend() {
        // TODO: stop VideoCapture, POST the file to /v1/clips, and announce
        // "Clip sent, analyzing." via TextToSpeech when the upload starts.
        pulse()
        statusText.text = getString(R.string.status_idle)
    }

    companion object {
        private const val REQUEST_CODE = 42
    }
}
